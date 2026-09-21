#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
iac.py — orquestrador de deploy de frontend estático para k3s/RKE2/Rancher.

Emula um pipeline de CI/CD executando localmente. Fluxo completo:

    preflight -> build -> package -> upload -> deploy -> verify -> prune

Sem dependências externas: só stdlib. Roda com `python iac.py` ou,
se você usa uv, `uv run deploy/iac.py` (o bloco PEP 723 acima resolve).

Uso rápido:
    python iac.py doctor            # só checa o ambiente
    python iac.py deploy --dry-run  # mostra o que faria
    python iac.py deploy            # pipeline completo
    python iac.py rollback          # volta a revisão anterior
    python iac.py status            # estado atual no cluster
    python iac.py init              # escreve Dockerfile/nginx/toml
    python iac.py install-key       # copia sua pubkey pro servidor

Nota sobre performance: este script é 99,9% espera de I/O (npm build, ssh,
docker build). Não há hot path de CPU em Python aqui, então otimização
vetorizada não se aplica — o que importa é robustez, idempotência e
número de round-trips SSH. O pipeline abre no máximo 5 conexões SSH;
com chave configurada isso é imperceptível, com senha vira 5 prompts.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    print("iac.py requer Python 3.11+ (tomllib).", file=sys.stderr)
    raise SystemExit(2)

__version__ = "2.1.0"

# ---------------------------------------------------------------------------
# Terminal / logging
# ---------------------------------------------------------------------------


def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    if os.name == "nt":
        # Windows 10+ suporta VT, mas precisa ser habilitado explicitamente
        # quando o processo não herda um console já em modo VT.
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            return False
    return True


class C:
    """Códigos ANSI, neutralizados quando o terminal não suporta."""

    _on = _supports_color()

    RESET = "\033[0m" if _on else ""
    BOLD = "\033[1m" if _on else ""
    DIM = "\033[2m" if _on else ""
    RED = "\033[31m" if _on else ""
    GREEN = "\033[32m" if _on else ""
    YELLOW = "\033[33m" if _on else ""
    BLUE = "\033[34m" if _on else ""
    CYAN = "\033[36m" if _on else ""
    GREY = "\033[90m" if _on else ""


_T0 = time.monotonic()


def _stamp() -> str:
    return f"{C.GREY}[{time.monotonic() - _T0:7.2f}s]{C.RESET}"


def info(msg: str) -> None:
    print(f"{_stamp()} {msg}", flush=True)


def step(msg: str) -> None:
    print(f"\n{_stamp()} {C.BOLD}{C.CYAN}==> {msg}{C.RESET}", flush=True)


def ok(msg: str) -> None:
    print(f"{_stamp()} {C.GREEN}  ok{C.RESET} {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"{_stamp()} {C.YELLOW}  !!{C.RESET} {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"{_stamp()} {C.RED}  xx{C.RESET} {msg}", file=sys.stderr, flush=True)


def detail(msg: str) -> None:
    print(f"{C.GREY}       {msg}{C.RESET}", flush=True)


class DeployError(RuntimeError):
    """Erro esperado do pipeline — vira mensagem limpa, não traceback."""


# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RemoteCfg:
    host: str = ""
    user: str = ""
    port: int = 22
    identity: str | None = None
    workdir: str = "/tmp/iac-deploy"
    connect_timeout: int = 10
    # Senha: preferir deploy/iac.local.toml (fora do git) ou a variável de
    # ambiente IAC_SSH_PASSWORD. Se preenchida, o transporte passa a ser
    # paramiko, porque o cliente OpenSSH do Windows não aceita senha por
    # stdin. Deixe vazio e use chave — é uma linha de setup a menos e um
    # segredo a menos no disco.
    password: str | None = None


@dataclass(frozen=True, slots=True)
class K8sCfg:
    namespace: str = ""
    deployment: str = ""
    container: str = ""  # vazio = autodetectar
    ingress_host: str = ""
    # Hosts do Ingress. Vazio = usa só ingress_host. Preencha para publicar
    # apex + www no mesmo Ingress.
    ingress_hosts: tuple[str, ...] = ()
    rollout_timeout: int = 180
    verify_paths: tuple[str, ...] = ("/",)
    verify_tries: int = 3
    # Trecho que a home DEVE conter. Status 200 sozinho nao prova nada: se o
    # Traefik rotear para o pod errado, a pagina padrao do nginx tambem
    # responde 200. Vazio desliga a checagem.
    verify_contains: str = ""


@dataclass(frozen=True, slots=True)
class BuildCfg:
    mode: str = "artifact"  # artifact | source
    command: tuple[str, ...] = ("npm", "run", "build")
    dist: str = ""
    dockerfile: str = "deploy/Dockerfile.runtime"
    nginx_conf: str = "deploy/nginx.conf"
    source_excludes: tuple[str, ...] = (
        "node_modules",
        "dist",
        ".git",
        ".idea",
        ".vscode",
        "__pycache__",
    )


@dataclass(frozen=True, slots=True)
class ImageCfg:
    repository: str = ""
    keep: int = 5
    containerd_ns: str = "k8s.io"


@dataclass(frozen=True, slots=True)
class Config:
    remote: RemoteCfg = field(default_factory=RemoteCfg)
    k8s: K8sCfg = field(default_factory=K8sCfg)
    build: BuildCfg = field(default_factory=BuildCfg)
    image: ImageCfg = field(default_factory=ImageCfg)

    @property
    def ssh_target(self) -> str:
        return f"{self.remote.user}@{self.remote.host}"


_SECTION_TYPES = {
    "remote": RemoteCfg,
    "k8s": K8sCfg,
    "build": BuildCfg,
    "image": ImageCfg,
}


def _read_toml(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def load_config(path: Path) -> Config:
    """Carrega iac.toml e, por cima, iac.local.toml (não versionado).

    A separação existe para segredos: `iac.toml` vai para o git, o
    `.local.toml` fica só na sua máquina e sobrescreve chave a chave.
    """
    local = path.with_name(f"{path.stem}.local{path.suffix}")

    raw: dict = {}
    for layer in (path, local):
        if not layer.exists():
            continue
        for section, values in _read_toml(layer).items():
            if isinstance(values, dict):
                raw.setdefault(section, {}).update(values)
            else:
                raw[section] = values

    if not raw:
        return Config()

    sections: dict[str, object] = {}
    for name, cls in _SECTION_TYPES.items():
        data = raw.get(name, {})
        if not isinstance(data, dict):
            raise DeployError(f"{path}: seção [{name}] deve ser uma tabela")
        valid = {f for f in cls.__dataclass_fields__}
        unknown = set(data) - valid
        if unknown:
            raise DeployError(
                f"{path}: chaves desconhecidas em [{name}]: {', '.join(sorted(unknown))}"
            )
        # tuplas precisam ser convertidas de list
        coerced = {
            k: tuple(v) if isinstance(v, list) else v for k, v in data.items()
        }
        sections[name] = cls(**coerced)

    return Config(**sections)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Execução de comandos
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Result:
    code: int
    out: str


class Shell:
    """Wrapper de subprocess com streaming, dry-run e contagem de SSH."""

    def __init__(self, cfg: Config, *, dry_run: bool = False, verbose: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.verbose = verbose
        self.ssh_calls = 0
        self._pk_client = None  # cliente paramiko, aberto sob demanda
        self.password = (
            os.environ.get("IAC_SSH_PASSWORD") or cfg.remote.password or None
        )

    # -- escolha de transporte -------------------------------------------

    @property
    def uses_password(self) -> bool:
        return bool(self.password)

    def close(self) -> None:
        if self._pk_client is not None:
            try:
                self._pk_client.close()
            finally:
                self._pk_client = None

    def _paramiko(self):
        """Cliente paramiko reutilizado entre chamadas.

        Uma conexão só para todo o pipeline: além de não pedir senha
        repetidas vezes, elimina 4 handshakes TCP+SSH.
        """
        if self._pk_client is not None:
            return self._pk_client
        try:
            import paramiko  # type: ignore
        except ModuleNotFoundError:
            raise DeployError(
                "senha configurada, mas paramiko não está instalado.\n"
                "       Opções:\n"
                "         uv run --with paramiko deploy/iac.py <comando>   "
                "(recomendado: não instala nada permanente)\n"
                "         python -m pip install paramiko\n"
                "       Ou remova a senha e use chave: "
                "python iac.py install-key"
            ) from None

        r = self.cfg.remote
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=r.host,
                port=r.port,
                username=r.user,
                password=self.password,
                timeout=r.connect_timeout,
                allow_agent=False,
                look_for_keys=False,
            )
        except Exception as exc:  # paramiko levanta várias subclasses
            raise DeployError(f"falha ao conectar em {self.cfg.ssh_target}: {exc}")

        self._pk_client = client
        return client

    @property
    def ssh_bin(self) -> str:
        """Resolvido sob demanda: `init` e `--help` não precisam de SSH."""
        exe = shutil.which("ssh")
        if exe is None:
            raise DeployError(
                "'ssh' não encontrado no PATH. No Windows 11: "
                "Configurações > Sistema > Componentes opcionais > "
                "Cliente OpenSSH."
            )
        return exe

    # -- local ----------------------------------------------------------

    def local(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        capture: bool = False,
        check: bool = True,
        quiet: bool = False,
    ) -> Result:
        exe = shutil.which(argv[0])
        if exe is None:
            raise DeployError(f"executável não encontrado no PATH: {argv[0]}")
        real = [exe, *argv[1:]]

        if self.verbose or self.dry_run:
            detail("$ " + " ".join(argv))
        if self.dry_run:
            return Result(0, "")

        return self._stream(real, cwd=cwd, capture=capture, check=check, quiet=quiet)

    # -- remoto ---------------------------------------------------------

    def _ssh_argv(self, *, tty: bool = False) -> list[str]:
        r = self.cfg.remote
        argv = [
            self.ssh_bin,
            "-o",
            "BatchMode=no",
            "-o",
            f"ConnectTimeout={r.connect_timeout}",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-p",
            str(r.port),
        ]
        if r.identity:
            argv += ["-i", str(Path(r.identity).expanduser())]
        if tty:
            argv.insert(1, "-tt")
        argv.append(self.cfg.ssh_target)
        return argv

    def remote_script(
        self,
        script: str,
        *,
        capture: bool = False,
        check: bool = True,
        tty: bool = False,
        label: str = "",
    ) -> Result:
        """Executa um script bash no servidor via `bash -s` (stdin)."""
        if self.verbose or self.dry_run:
            detail(f"$ ssh {self.cfg.ssh_target} bash -s   # {label or 'script'}")
            if self.verbose:
                for line in script.strip().splitlines():
                    detail("  | " + line)
        if self.dry_run:
            return Result(0, "")

        return self._remote(
            "bash -s",
            script.encode("utf-8"),
            capture=capture,
            check=check,
            tty=tty,
        )

    def remote_binary(
        self,
        command: str,
        payload: bytes,
        *,
        check: bool = True,
        label: str = "",
    ) -> Result:
        """Executa um comando remoto alimentando stdin com bytes crus.

        É assim que o tarball atravessa: o Python escreve bytes direto no
        canal. Não passa por PowerShell, então não há a corrupção clássica
        de `docker save | ssh ...` no Windows.
        """
        if self.verbose or self.dry_run:
            detail(
                f"$ ssh {self.cfg.ssh_target} '{command}'   "
                f"# stdin={len(payload)} bytes ({label})"
            )
        if self.dry_run:
            return Result(0, "")

        return self._remote(command, payload, check=check)

    def interactive(self, command: str) -> Result:
        """Roda um comando remoto com TTY e console herdado.

        Necessário quando o comando remoto precisa LER do teclado (sudo
        pedindo senha). Não dá para fazer isso com `bash -s`: o script já
        ocupa o stdin, e o sudo acabaria consumindo as linhas do próprio
        script como tentativas de senha.
        """
        if self.dry_run:
            detail(f"$ ssh -tt {self.cfg.ssh_target} '{command}'")
            return Result(0, "")

        self.ssh_calls += 1
        argv = self._ssh_argv(tty=True) + [command]
        proc = subprocess.run(argv)  # stdin/stdout/stderr herdados do console
        return Result(proc.returncode, "")

    def _remote(
        self,
        command: str,
        stdin_data: bytes,
        *,
        capture: bool = False,
        check: bool = True,
        tty: bool = False,
    ) -> Result:
        self.ssh_calls += 1
        if self.uses_password:
            return self._exec_paramiko(
                command, stdin_data, capture=capture, check=check
            )
        argv = self._ssh_argv(tty=tty) + [command]
        return self._stream(
            argv, capture=capture, check=check, stdin_bytes=stdin_data, quiet=capture
        )

    def _exec_paramiko(
        self,
        command: str,
        stdin_data: bytes,
        *,
        capture: bool,
        check: bool,
    ) -> Result:
        client = self._paramiko()
        transport = client.get_transport()
        if transport is None:
            raise DeployError("conexão SSH caiu")

        chan = transport.open_session()
        chan.set_combine_stderr(True)
        chan.settimeout(None)
        chan.exec_command(command)

        buf = bytearray()
        pending = bytearray()

        def _drain() -> None:
            # Ler em paralelo ao envio evita deadlock: o remoto pode encher
            # o buffer de stdout enquanto ainda estamos escrevendo stdin.
            while True:
                data = chan.recv(65536)
                if not data:
                    break
                buf.extend(data)
                pending.extend(data)

        reader = threading.Thread(target=_drain, daemon=True)
        reader.start()

        try:
            if stdin_data:
                chan.sendall(stdin_data)
        except OSError:
            pass
        finally:
            chan.shutdown_write()

        reader.join()
        code = chan.recv_exit_status()
        chan.close()

        text = bytes(buf).decode("utf-8", errors="replace")
        if not capture:
            for line in text.splitlines():
                detail(line)

        if check and code != 0:
            raise DeployError(f"comando remoto falhou (exit {code}): {command}")
        return Result(code, text)

    # -- motor ----------------------------------------------------------

    def _stream(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        capture: bool = False,
        check: bool = True,
        quiet: bool = False,
        stdin_text: str | None = None,
        stdin_bytes: bytes | None = None,
    ) -> Result:
        stdin_data: bytes | None = None
        if stdin_bytes is not None:
            stdin_data = stdin_bytes
        elif stdin_text is not None:
            stdin_data = stdin_text.encode("utf-8")

        proc = subprocess.Popen(
            list(argv),
            cwd=str(cwd) if cwd else None,
            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        if stdin_data is not None:
            assert proc.stdin is not None
            try:
                proc.stdin.write(stdin_data)
            except BrokenPipeError:
                pass
            finally:
                proc.stdin.close()

        chunks: list[str] = []
        assert proc.stdout is not None
        for raw in io.TextIOWrapper(proc.stdout, encoding="utf-8", errors="replace"):
            line = raw.rstrip("\r\n")
            if capture:
                chunks.append(line)
            if not quiet:
                detail(line)

        code = proc.wait()
        out = "\n".join(chunks)

        if check and code != 0:
            raise DeployError(
                f"comando falhou (exit {code}): {' '.join(map(str, argv[:3]))} ..."
            )
        return Result(code, out)


# ---------------------------------------------------------------------------
# Estado persistente
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class State:
    path: Path
    history: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "State":
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return cls(path=path, history=list(data.get("history", [])))
            except (json.JSONDecodeError, OSError):
                warn(f"estado ilegível em {path.name}, começando do zero")
        return cls(path=path)

    def record(self, entry: dict) -> None:
        self.history.append(entry)
        self.history = self.history[-50:]
        try:
            self.path.write_text(
                json.dumps({"history": self.history}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            warn(f"não consegui gravar {self.path.name}: {exc}")

    @property
    def last(self) -> dict | None:
        return self.history[-1] if self.history else None


# ---------------------------------------------------------------------------
# Contexto do pipeline
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Context:
    cfg: Config
    sh: Shell
    root: Path
    state: State
    args: argparse.Namespace

    tag: str = ""
    digest: str = ""
    tarball: bytes = b""
    container: str = ""
    needs_tty: bool = False
    facts: dict[str, str] = field(default_factory=dict)
    deployed: bool = False

    @property
    def image(self) -> str:
        return f"{self.cfg.image.repository}:{self.tag}"


# ---------------------------------------------------------------------------
# Templates de scripts remotos
# ---------------------------------------------------------------------------


def render(template: str, **kw: object) -> str:
    """Substitui @@CHAVE@@ — evita conflito entre f-string e sintaxe bash."""
    out = template
    for key, value in kw.items():
        out = out.replace(f"@@{key.upper()}@@", str(value))
    leftover = re.findall(r"@@[A-Z_]+@@", out)
    if leftover:
        raise DeployError(f"placeholder não resolvido: {', '.join(set(leftover))}")
    return out


SUDO_PRELUDE = r"""
# Resolve como chamar o sudo. Se houver NOPASSWD, usa direto. Senão, e se o
# orquestrador tiver uma senha, monta um helper SUDO_ASKPASS temporário —
# é o único jeito de o sudo receber a senha sem TTY. O helper vive em
# mktemp com modo 700 e é removido no EXIT.
SUDO="sudo"
if ! sudo -n true 2>/dev/null; then
  if [ -n "${IAC_SUDO_PW:-}" ]; then
    _ASKPASS="$(mktemp)"
    cat > "$_ASKPASS" <<'ASKPASS_EOF'
#!/bin/sh
printf '%s\n' "$IAC_SUDO_PW"
ASKPASS_EOF
    chmod 700 "$_ASKPASS"
    export SUDO_ASKPASS="$_ASKPASS"
    SUDO="sudo -A"
    trap 'rm -f "$_ASKPASS"' EXIT
  fi
fi
"""


SUDO_PW_EXPORT = r"""
IAC_SUDO_PW="$(printf '%s' '@@SUDO_PW_B64@@' | base64 -d)"
export IAC_SUDO_PW
"""


PREFLIGHT_SH = r"""
set -uo pipefail
emit() { printf '::%s::%s\n' "$1" "$2"; }

if [ -r /etc/os-release ]; then
  . /etc/os-release
  emit os "$PRETTY_NAME"
fi

if command -v docker >/dev/null 2>&1; then
  emit docker "$(docker --version 2>/dev/null | head -1)"
  docker info >/dev/null 2>&1 || emit docker_perm "sem permissao (falta grupo docker?)"
else
  emit error "docker nao encontrado no servidor"
fi

if command -v k3s >/dev/null 2>&1; then
  CTR_BIN="$(command -v k3s)"
  CTR_ARGS=(ctr -n "@@CTR_NS@@")
  emit k3s "$(k3s --version 2>/dev/null | head -1)"
  emit runtime k3s
elif [ -x /var/lib/rancher/rke2/bin/ctr ]; then
  CTR_BIN="/var/lib/rancher/rke2/bin/ctr"
  CTR_ARGS=(--address /run/k3s/containerd/containerd.sock --namespace "@@CTR_NS@@")
  emit runtime rke2
  emit rke2 "$(/var/lib/rancher/rke2/bin/rke2 --version 2>/dev/null | head -1)"
else
  emit error "nem k3s nem o ctr do RKE2 foram encontrados"
fi

if command -v kubectl >/dev/null 2>&1; then
  emit kubectl "$(kubectl version --client -o json 2>/dev/null | tr -d '\n' | head -c 120)"
else
  emit error "kubectl nao encontrado no PATH do usuario"
fi

emit sudo_impl "$(sudo --version 2>&1 | head -1)"
if [ -n "${CTR_BIN:-}" ] && sudo -n "$CTR_BIN" "${CTR_ARGS[@]}" images ls -q >/dev/null 2>&1; then
  emit sudo nopasswd
elif sudo -n true 2>/dev/null; then
  emit sudo nopasswd_full
else
  emit sudo needs_password
fi

NS="@@NAMESPACE@@"
DEP="@@DEPLOYMENT@@"

if ! kubectl get ns "$NS" >/dev/null 2>&1; then
  emit error "namespace $NS nao existe ou kubeconfig sem acesso"
else
  emit namespace "$NS"
fi

if ! kubectl -n "$NS" get deploy "$DEP" >/dev/null 2>&1; then
  emit error "deployment $DEP nao existe no namespace $NS"
else
  emit container "$(kubectl -n "$NS" get deploy "$DEP" -o jsonpath='{.spec.template.spec.containers[0].name}')"
  emit current_image "$(kubectl -n "$NS" get deploy "$DEP" -o jsonpath='{.spec.template.spec.containers[0].image}')"
  emit pull_policy "$(kubectl -n "$NS" get deploy "$DEP" -o jsonpath='{.spec.template.spec.containers[0].imagePullPolicy}')"
  emit replicas "$(kubectl -n "$NS" get deploy "$DEP" -o jsonpath='{.status.readyReplicas}/{.spec.replicas}')"
fi

emit disk "$(df -h /var/lib/rancher --output=pcent 2>/dev/null | tail -1 | tr -d ' ')"
exit 0
"""


DEPLOY_SH = r"""
set -euo pipefail

WORKDIR="@@WORKDIR@@"
IMAGE="@@IMAGE@@"
NS="@@NAMESPACE@@"
DEP="@@DEPLOYMENT@@"
CONTAINER="@@CONTAINER@@"
CTR_NS="@@CTR_NS@@"
DOCKERFILE="@@DOCKERFILE@@"

@@SUDO_SETUP@@

cd "$WORKDIR"

if command -v k3s >/dev/null 2>&1; then
  CTR_BIN="$(command -v k3s)"
  CTR_ARGS=(ctr -n "$CTR_NS")
  RUNTIME="k3s"
elif [ -x /var/lib/rancher/rke2/bin/ctr ]; then
  CTR_BIN="/var/lib/rancher/rke2/bin/ctr"
  CTR_ARGS=(--address /run/k3s/containerd/containerd.sock --namespace "$CTR_NS")
  RUNTIME="rke2"
else
  echo "containerd do k3s/RKE2 nao encontrado" >&2
  exit 12
fi

echo "--- docker build ---"
docker build --pull=false -f "$DOCKERFILE" -t "$IMAGE" .

echo "--- sideload no containerd do $RUNTIME (ns=$CTR_NS) ---"
docker save "$IMAGE" | $SUDO "$CTR_BIN" "${CTR_ARGS[@]}" images import -

echo "--- patch atômico: imagem + pullPolicy + anotações ---"
# As anotações vão no POD TEMPLATE, não no metadata do Deployment. Isso é
# deliberado: anotação no template muda o hash do ReplicaSet e força um
# rollout mesmo quando a imagem é idêntica (mesmo efeito de `rollout
# restart`). Anotação no Deployment não dispara nada. Como imagem,
# pullPolicy e anotação vão num único patch, o resultado é UM rollout.
STAMP="$(date -Iseconds)"
kubectl -n "$NS" patch deployment "$DEP" --type=strategic -p "{
  \"spec\": {
    \"template\": {
      \"metadata\": {
        \"annotations\": {
          \"iac.sgt/deployed-at\": \"$STAMP\",
          \"iac.sgt/deployed-by\": \"@@OPERATOR@@\",
          \"iac.sgt/artifact-sha256\": \"@@DIGEST@@\"
        }
      },
      \"spec\": {
        \"containers\": [{
          \"name\": \"$CONTAINER\",
          \"image\": \"$IMAGE\",
          \"imagePullPolicy\": \"IfNotPresent\"
        }]
      }
    }
  }
}" >/dev/null

echo "--- aguardando rollout ---"
if ! kubectl -n "$NS" rollout status "deployment/$DEP" --timeout=@@TIMEOUT@@s; then
  echo "ROLLOUT_FAILED"

  echo "--- pods ---"
  kubectl -n "$NS" get pods -o wide

  # O que realmente diz por que o container morreu. `--previous` é essencial:
  # num CrashLoopBackOff o container atual pode estar entre reinícios e sem
  # log nenhum; o log útil é o da encarnação anterior.
  SEL="$(kubectl -n "$NS" get deploy "$DEP" -o jsonpath='{.spec.selector.matchLabels}' \
        | tr -d '{}"' | tr ',' '\n' | paste -sd, -)"
  for POD in $(kubectl -n "$NS" get pods -l "$SEL" \
               --field-selector=status.phase!=Running -o name 2>/dev/null); do
    echo "--- logs $POD (atual) ---"
    kubectl -n "$NS" logs "$POD" --tail=30 --all-containers 2>&1 | tail -30
    echo "--- logs $POD (anterior) ---"
    kubectl -n "$NS" logs "$POD" --previous --tail=30 --all-containers 2>&1 | tail -30
  done

  echo "--- eventos recentes ---"
  kubectl -n "$NS" get events --sort-by=.lastTimestamp 2>/dev/null | tail -12

  exit 20
fi

echo "--- limpando workdir ---"
rm -rf "$WORKDIR"
echo "DEPLOY_OK $IMAGE"
"""


VERIFY_SH = r"""
set -uo pipefail
NS="@@NAMESPACE@@"
DEP="@@DEPLOYMENT@@"
HOST="@@INGRESS_HOST@@"
EXPECT="@@IMAGE@@"
TRIES=@@TRIES@@
CONTAINS="@@CONTAINS@@"

LIVE="$(kubectl -n "$NS" get deploy "$DEP" -o jsonpath='{.spec.template.spec.containers[0].image}')"
# EXPECT vazio = `--only verify` sem --tag: vira health check da imagem que
# estiver publicada, em vez de falhar comparando com uma tag inexistente.
if [ -n "$EXPECT" ] && [ "$LIVE" != "$EXPECT" ]; then
  echo "::error::deployment esta com $LIVE, esperado $EXPECT"
  exit 30
fi
echo "::image::$LIVE"

READY="$(kubectl -n "$NS" get deploy "$DEP" -o jsonpath='{.status.readyReplicas}')"
echo "::ready::${READY:-0}"

ERRFILE="$(mktemp)"
trap 'rm -f "$ERRFILE"' EXIT
RC=0

for P in @@PATHS@@; do
  CODE=""
  CURL_RC=0
  I=1
  while [ "$I" -le "$TRIES" ]; do
    if [ -n "$HOST" ]; then
      CODE="$(curl -sS -o /dev/null -w '%{http_code}' \
              -L --max-redirs 3 --max-time 8 \
              --resolve "$HOST:80:127.0.0.1" \
              "http://$HOST$P" 2>"$ERRFILE")"
      CURL_RC=$?
    else
      kubectl -n "$NS" exec "deployment/$DEP" -- \
        wget -qO- "http://127.0.0.1$P" >/dev/null 2>"$ERRFILE"
      CURL_RC=$?
      [ "$CURL_RC" -eq 0 ] && CODE=200 || CODE=000
    fi
    case "$CURL_RC:$CODE" in
      0:2*|0:3*) break ;;
    esac
    I=$((I + 1))
    [ "$I" -le "$TRIES" ] && sleep 2
  done

  ERR="$(tr '\n' ' ' < "$ERRFILE" | cut -c1-100)"
  echo "::route::$P|${CODE:-000}|$CURL_RC|$ERR|$((I > TRIES ? TRIES : I))"
  case "$CURL_RC:$CODE" in
    0:2*|0:3*) ;;
    *) RC=31 ;;
  esac
done

if [ -n "$CONTAINS" ]; then
  if [ -n "$HOST" ]; then
    BODY="$(curl -sS -L --max-redirs 3 --max-time 8 \
            --resolve "$HOST:80:127.0.0.1" "http://$HOST/" 2>/dev/null | head -c 262144)"
  else
    BODY="$(kubectl -n "$NS" exec "deployment/$DEP" -- \
            wget -qO- http://127.0.0.1/ 2>/dev/null | head -c 262144)"
  fi
  case "$BODY" in
    *"$CONTAINS"*)
      echo "::content::ok — a home contém \"$CONTAINS\"" ;;
    *"Welcome to nginx"*)
      echo "::content::a home devolveu a PAGINA PADRAO DO NGINX — o pod nao recebeu o conteudo publicado"
      RC=32 ;;
    *)
      echo "::content::a home nao contém \"$CONTAINS\" — conteudo inesperado"
      RC=32 ;;
  esac
fi

if [ "$RC" -ne 0 ]; then
  {
    if [ -n "$HOST" ]; then
      echo "--- ingress ---"
      kubectl -n "$NS" get ingress -o wide 2>&1
    else
      echo "--- verificacao direta no pod (sem ingress_host configurado) ---"
    fi
    echo "--- services e endpoints ---"
    kubectl -n "$NS" get svc 2>&1
    kubectl -n "$NS" get endpoints 2>&1
    echo "--- selector do deployment ---"
    kubectl -n "$NS" get deploy "$DEP" \
      -o jsonpath='{.spec.selector.matchLabels}{"\n"}' 2>&1
    echo "--- labels reais dos pods ---"
    kubectl -n "$NS" get pods --show-labels 2>&1
    echo "--- primeiros bytes da home ---"
    if [ -n "$HOST" ]; then
      curl -sS -L --max-time 8 --resolve "$HOST:80:127.0.0.1" "http://$HOST/" 2>&1 | head -c 300
    else
      kubectl -n "$NS" exec "deployment/$DEP" -- wget -qO- http://127.0.0.1/ 2>&1 | head -c 300
    fi
  } | sed 's/^/::diag::/'
fi

exit $RC
"""


PRUNE_SH = r"""
set -uo pipefail
REPO="@@REPO@@"
CTR_NS="@@CTR_NS@@"
KEEP=@@KEEP@@

@@SUDO_SETUP@@

if command -v k3s >/dev/null 2>&1; then
  CTR_BIN="$(command -v k3s)"
  CTR_ARGS=(ctr -n "$CTR_NS")
elif [ -x /var/lib/rancher/rke2/bin/ctr ]; then
  CTR_BIN="/var/lib/rancher/rke2/bin/ctr"
  CTR_ARGS=(--address /run/k3s/containerd/containerd.sock --namespace "$CTR_NS")
else
  echo "containerd do k3s/RKE2 nao encontrado" >&2
  exit 12
fi

# O containerd normaliza a referencia para docker.io/<repo>:<tag>. Ancorar
# o grep no inicio da linha (^) nunca casa e o prune vira no-op silencioso.
MAPFILE="$($SUDO "$CTR_BIN" "${CTR_ARGS[@]}" images ls -q | grep -E "(^|/)${REPO}:" | sort -r || true)"
TOTAL="$(printf '%s\n' "$MAPFILE" | sed '/^$/d' | wc -l)"
echo "::total::$TOTAL"

printf '%s\n' "$MAPFILE" | sed '/^$/d' | tail -n +$((KEEP + 1)) | while read -r img; do
  echo "::removing::$img"
  $SUDO "$CTR_BIN" "${CTR_ARGS[@]}" images rm "$img" >/dev/null 2>&1 || true
  docker rmi "$img" >/dev/null 2>&1 || true
done

docker image prune -f >/dev/null 2>&1 || true
exit 0
"""


STATUS_SH = r"""
set -uo pipefail
NS="@@NAMESPACE@@"
DEP="@@DEPLOYMENT@@"
kubectl -n "$NS" get deploy "$DEP" -o wide
echo
kubectl -n "$NS" get pods -o wide
echo
echo "--- anotações do iac ---"
kubectl -n "$NS" get deploy "$DEP" -o jsonpath='{.spec.template.metadata.annotations}' \
  | tr ',' '\n' | grep -i 'iac.sgt' || echo "(nenhuma)"
echo
echo "--- histórico de rollout ---"
kubectl -n "$NS" rollout history "deployment/$DEP"
"""


ROLLBACK_SH = r"""
set -euo pipefail
NS="@@NAMESPACE@@"
DEP="@@DEPLOYMENT@@"
REV="@@REVISION@@"

if [ "$REV" = "0" ]; then
  kubectl -n "$NS" rollout undo "deployment/$DEP"
else
  kubectl -n "$NS" rollout undo "deployment/$DEP" --to-revision="$REV"
fi
kubectl -n "$NS" rollout status "deployment/$DEP" --timeout=@@TIMEOUT@@s
kubectl -n "$NS" get deploy "$DEP" -o jsonpath='{.spec.template.spec.containers[0].image}'
echo
"""


SUDOERS_SH = r"""
set -euo pipefail

CTR_NS="@@CTR_NS@@"
if command -v k3s >/dev/null 2>&1; then
  RUNTIME_BIN="$(command -v k3s)"
  CHECK_ARGS=(ctr -n "$CTR_NS" images ls -q)
  RUNTIME="k3s"
elif [ -x /var/lib/rancher/rke2/bin/ctr ]; then
  RUNTIME_BIN="/var/lib/rancher/rke2/bin/ctr"
  CHECK_ARGS=(--address /run/k3s/containerd/containerd.sock --namespace "$CTR_NS" images ls -q)
  RUNTIME="rke2"
else
  echo "nem k3s nem o ctr do RKE2 foram encontrados" >&2
  exit 2
fi

echo "--- implementacao do sudo ---"
sudo --version 2>&1 | head -2 || true

TMP="$(mktemp)"
chmod 600 "$TMP"

if [ "@@MODE@@" = "full" ]; then
  printf '%s ALL=(ALL) NOPASSWD: ALL\n' "@@USER@@" > "$TMP"
else
  # SEM curinga nos argumentos. O Ubuntu 25.10 usa sudo-rs, que nao suporta
  # `*` em posicao de argumento: a regra passa no visudo mas nunca casa.
  # Comando sem argumentos em sudoers ja significa "qualquer argumento".
  printf '%s ALL=(root) NOPASSWD: %s\n' "@@USER@@" "$RUNTIME_BIN" > "$TMP"
fi

echo "--- regra a instalar ---"
cat "$TMP"

echo "--- validando sintaxe ANTES de instalar ---"
sudo visudo -c -f "$TMP"

echo "--- instalando em /etc/sudoers.d/iac-containerd ---"
sudo install -m 0440 -o root -g root "$TMP" /etc/sudoers.d/iac-containerd
rm -f "$TMP"

if ! id -nG "@@USER@@" | tr ' ' '\n' | grep -qx docker; then
  echo "--- adicionando @@USER@@ ao grupo docker ---"
  sudo usermod -aG docker "@@USER@@"
  echo "O grupo docker sera reconhecido nas proximas conexoes SSH."
fi

# CRUCIAL: descartar o timestamp do sudo. Sem isso, o teste abaixo passaria
# por causa da credencial em cache da senha que voce acabou de digitar, e
# nao por causa da regra — mascarando uma regra que nao funciona.
echo "--- descartando cache de credencial para testar de verdade ---"
sudo -k

echo "--- conferindo (sem cache) ---"
if sudo -n "$RUNTIME_BIN" "${CHECK_ARGS[@]}" >/dev/null 2>&1; then
  echo "SUDOERS_OK $RUNTIME ($RUNTIME_BIN)"
else
  echo "SUDOERS_FAIL" >&2
  echo "A regra foi instalada mas o sudo ainda exige senha para $RUNTIME_BIN." >&2
  echo "Tente a regra ampla: python iac.py setup-sudo --full" >&2
  exit 3
fi
"""


# ---------------------------------------------------------------------------
# Empacotamento determinístico
# ---------------------------------------------------------------------------


def sudo_setup(ctx: "Context") -> str:
    """Monta o bloco de configuração do sudo para os scripts remotos."""
    prelude = SUDO_PRELUDE
    if ctx.sh.password:
        import base64

        b64 = base64.b64encode(ctx.sh.password.encode("utf-8")).decode("ascii")
        prelude = render(SUDO_PW_EXPORT, sudo_pw_b64=b64) + prelude
    return prelude


_EPOCH = 1577836800  # 2020-01-01T00:00:00Z


def _normalize(ti: tarfile.TarInfo) -> tarfile.TarInfo:
    """Zera metadados voláteis para que o mesmo conteúdo gere o mesmo sha256."""
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = "root"
    # Data fixa (2020-01-01) em vez de 0: mantem o build reproduzivel — que e
    # o que da cache hit nas camadas do Docker — sem gerar
    # `Last-Modified: Thu, 01 Jan 1970`, que alguns proxies e CDNs tratam
    # como suspeito.
    ti.mtime = _EPOCH
    ti.mode = 0o755 if ti.isdir() else 0o644
    return ti


def _walk_sorted(base: Path, excludes: Iterable[str] = ()) -> list[Path]:
    skip = set(excludes)
    found: list[Path] = []
    for path in sorted(base.rglob("*")):
        rel_parts = path.relative_to(base).parts
        if any(part in skip for part in rel_parts):
            continue
        found.append(path)
    return found


def build_tarball(ctx: Context) -> tuple[bytes, int]:
    """Monta o tar.gz do contexto de build. Retorna (bytes, n_arquivos)."""
    cfg = ctx.cfg
    root = ctx.root
    raw = io.BytesIO()
    count = 0

    # tar sem compressão primeiro: o header do gzip carrega um mtime que
    # mudaria a cada execução e quebraria o hash de conteúdo.
    with tarfile.open(fileobj=raw, mode="w") as tar:
        dockerfile = root / cfg.build.dockerfile
        nginx_conf = root / cfg.build.nginx_conf
        if not dockerfile.exists():
            raise DeployError(
                f"Dockerfile não encontrado: {dockerfile}\n"
                f"       rode `python deploy/iac.py init` para gerá-lo."
            )
        tar.add(dockerfile, arcname="Dockerfile", filter=_normalize)
        count += 1

        if cfg.build.mode == "artifact":
            if not nginx_conf.exists():
                raise DeployError(f"nginx.conf não encontrado: {nginx_conf}")
            tar.add(nginx_conf, arcname="nginx.conf", filter=_normalize)
            count += 1

            dist = root / cfg.build.dist
            if not dist.is_dir():
                raise DeployError(
                    f"dist não encontrado: {dist}\n"
                    f"       o build rodou? confira build.dist no iac.toml."
                )
            for path in _walk_sorted(dist):
                arc = "browser/" + path.relative_to(dist).as_posix()
                tar.add(path, arcname=arc, filter=_normalize, recursive=False)
                if path.is_file():
                    count += 1
        else:  # source
            for path in _walk_sorted(root, cfg.build.source_excludes):
                arc = path.relative_to(root).as_posix()
                if arc == "Dockerfile":
                    continue
                tar.add(path, arcname=arc, filter=_normalize, recursive=False)
                if path.is_file():
                    count += 1

    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6, mtime=0) as gz:
        gz.write(raw.getvalue())

    return buf.getvalue(), count


# ---------------------------------------------------------------------------
# Etapas do pipeline
# ---------------------------------------------------------------------------


def _check_dockerfile_mode(ctx: Context) -> None:
    """Pega cedo o erro mais comum: Dockerfile multi-stage no modo artifact.

    No modo artifact só o `browser/` viaja. Um Dockerfile que começa com
    `COPY package.json` morre no `docker build` lá no servidor, depois de
    já ter gasto build local, upload e o pull do node:22-alpine. Ler duas
    linhas aqui economiza esse ciclo inteiro.
    """
    path = ctx.root / ctx.cfg.build.dockerfile
    if not path.exists():
        raise DeployError(
            f"Dockerfile não encontrado: {path}\n"
            f"       rode `python deploy/iac.py init` para gerá-lo."
        )

    body = path.read_text(encoding="utf-8", errors="replace")
    multistage = bool(re.search(r"^\s*FROM\s+\S+\s+AS\s+", body, re.M | re.I))
    needs_source = "package.json" in body

    if ctx.cfg.build.mode == "artifact" and (multistage or needs_source):
        raise DeployError(
            f"{ctx.cfg.build.dockerfile} é multi-stage (builda a partir do "
            f"código-fonte), mas build.mode = 'artifact' envia apenas o "
            f"conteúdo de {ctx.cfg.build.dist}.\n"
            f"       O `docker build` falharia com 'stat package.json: file "
            f"does not exist'.\n"
            f"       Escolha um dos dois:\n"
            f"         dockerfile = \"deploy/Dockerfile.runtime\"   (recomendado)\n"
            f"         mode = \"source\"  +  dockerfile = \"deploy/Dockerfile.source\""
        )

    if ctx.cfg.build.mode == "source" and not multistage:
        warn(
            f"{ctx.cfg.build.dockerfile} não parece multi-stage, mas "
            f"mode = 'source' — confira se é o Dockerfile certo"
        )

    ok(f"dockerfile: {ctx.cfg.build.dockerfile} (mode={ctx.cfg.build.mode})")


def step_preflight(ctx: Context) -> None:
    step("preflight — checando ambiente local e remoto")

    # local
    if ctx.sh.uses_password:
        ok("transporte: paramiko (senha) — conexão única reaproveitada")
    else:
        if shutil.which("ssh") is None:
            raise DeployError("'ssh' ausente no PATH")
        ok(f"transporte: OpenSSH ({shutil.which('ssh')})")

    if ctx.cfg.build.mode == "artifact" and not ctx.args.skip_build:
        npm = shutil.which(ctx.cfg.build.command[0])
        if npm is None:
            raise DeployError(f"'{ctx.cfg.build.command[0]}' ausente no PATH")
        ok(f"{ctx.cfg.build.command[0]}: {npm}")

    _check_dockerfile_mode(ctx)

    # remoto
    script = render(
        PREFLIGHT_SH,
        namespace=ctx.cfg.k8s.namespace,
        deployment=ctx.cfg.k8s.deployment,
        ctr_ns=ctx.cfg.image.containerd_ns,
    )
    res = ctx.sh.remote_script(script, capture=True, check=False, label="preflight")
    if ctx.sh.dry_run:
        ok("(dry-run) preflight remoto pulado")
        ctx.container = ctx.cfg.k8s.container or ctx.cfg.k8s.deployment
        return

    errors: list[str] = []
    for line in res.out.splitlines():
        m = re.match(r"^::([a-z0-9_]+)::(.*)$", line.strip())
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        if key == "error":
            errors.append(value)
        else:
            ctx.facts[key] = value

    if ctx.facts.get("sudo") in ("nopasswd", "nopasswd_full"):
        ctx.facts["sudo"] = (
            f"containerd do {ctx.facts.get('runtime', 'cluster')} liberado sem senha"
            if ctx.facts["sudo"] == "nopasswd"
            else "sudo irrestrito sem senha"
        )

    for key in ("os", "docker", "runtime", "k3s", "rke2", "sudo", "namespace", "current_image", "replicas", "disk"):
        if key in ctx.facts:
            ok(f"{key}: {ctx.facts[key]}")

    if ctx.facts.get("sudo") == "needs_password" and not ctx.sh.uses_password:
        # Não dá para contornar isso com `ssh -tt`: o script do deploy chega
        # pelo próprio stdin, então o sudo leria as linhas do script como
        # tentativas de senha e estouraria em 3 falhas. É bloqueante.
        errors.append(
            "sudo pede senha no servidor e o import da imagem no containerd "
            "precisa de root.\n"
            "       Resolva uma vez com:  python iac.py setup-sudo\n"
            "       (libera somente o binário do containerd e pede sua senha uma vez)"
        )

    if "docker_perm" in ctx.facts:
        errors.append(
            "usuário remoto sem acesso ao Docker.\n"
            "       Rode uma vez:  python iac.py setup-sudo\n"
            "       (o comando adiciona o usuário ao grupo docker)"
        )

    policy = ctx.facts.get("pull_policy", "")
    if policy and policy != "IfNotPresent":
        warn(f"imagePullPolicy atual é '{policy}' — será corrigido no deploy")

    detected = ctx.facts.get("container", "")
    ctx.container = ctx.cfg.k8s.container or detected or ctx.cfg.k8s.deployment
    ok(f"container alvo: {ctx.container}")

    if errors:
        for e in errors:
            fail(e)
        raise DeployError("preflight reprovou — corrija os itens acima")


def step_build(ctx: Context) -> None:
    if ctx.args.skip_build:
        step("build — pulado (--skip-build)")
        return
    if ctx.cfg.build.mode == "source":
        step("build — pulado (mode=source, o build ocorre no servidor)")
        return

    step(f"build — {' '.join(ctx.cfg.build.command)}")
    ctx.sh.local(list(ctx.cfg.build.command), cwd=ctx.root)

    dist = ctx.root / ctx.cfg.build.dist
    if not ctx.sh.dry_run:
        if not dist.is_dir():
            raise DeployError(f"build terminou mas {dist} não existe")
        files = [p for p in dist.rglob("*") if p.is_file()]
        size = sum(p.stat().st_size for p in files)
        ok(f"{len(files)} arquivos, {size / 1024:.0f} kB em {ctx.cfg.build.dist}")

        if not (dist / "index.html").exists():
            raise DeployError(f"{ctx.cfg.build.dist}/index.html não foi gerado pelo build")


def step_package(ctx: Context) -> None:
    step("package — montando contexto de build determinístico")
    if ctx.sh.dry_run:
        ctx.tag = "dryrun-0000000000"
        ctx.digest = "0" * 64
        ok("(dry-run) tarball não gerado")
        return

    payload, count = build_tarball(ctx)
    ctx.tarball = payload
    ctx.digest = hashlib.sha256(payload).hexdigest()
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
    ctx.tag = f"{ts}-{ctx.digest[:8]}"

    ok(f"{count} arquivos, {len(payload) / 1024:.1f} kB comprimidos")
    ok(f"sha256: {ctx.digest[:16]}…")
    ok(f"tag: {ctx.tag}")

    last = ctx.state.last
    if last and last.get("digest") == ctx.digest and not ctx.args.force:
        warn(
            f"conteúdo idêntico ao deploy de {last.get('at')} "
            f"({last.get('tag')}). Use --force para publicar mesmo assim."
        )
        raise DeployError("nada mudou — abortando")


def step_upload(ctx: Context) -> None:
    wd = ctx.cfg.remote.workdir
    step(f"upload — enviando contexto para {ctx.cfg.ssh_target}:{wd}")
    cmd = f"rm -rf {wd} && mkdir -p {wd} && tar -xzf - -C {wd} && ls -la {wd}"
    ctx.sh.remote_binary(cmd, ctx.tarball, label="tar.gz")
    ok(f"{len(ctx.tarball) / 1024:.1f} kB transferidos")


def step_deploy(ctx: Context) -> None:
    step(f"deploy — build + sideload + rollout ({ctx.image})")
    script = render(
        DEPLOY_SH,
        workdir=ctx.cfg.remote.workdir,
        image=ctx.image,
        namespace=ctx.cfg.k8s.namespace,
        deployment=ctx.cfg.k8s.deployment,
        container=ctx.container,
        ctr_ns=ctx.cfg.image.containerd_ns,
        dockerfile="Dockerfile",
        sudo_setup=sudo_setup(ctx),
        operator=f"{os.environ.get('USERNAME') or os.environ.get('USER') or '?'}@iac",
        digest=ctx.digest or "unknown",
        timeout=ctx.cfg.k8s.rollout_timeout,
    )
    ctx.sh.remote_script(script, tty=ctx.needs_tty, label="deploy")
    ctx.deployed = True
    ok(f"rollout concluído: {ctx.image}")


_HTTP_HINTS = {
    "404": "sem router para esse host — Ingress ausente, ou o backend dele aponta para um Service inexistente",
    "502": "backend recusou a conexão — pod no ar mas não escutando na porta?",
    "503": "Service sem endpoints prontos — selector do Service não casa "
           "com os labels do pod?",
    "504": "backend não respondeu a tempo",
}

_CURL_ERRORS = {
    "6": "DNS não resolveu",
    "7": "conexão recusada (nada escutando na 80?)",
    "28": "timeout — o Traefik pode estar roteando para um pod em terminação",
    "47": "excesso de redirects (loop no try_files?)",
    "52": "resposta vazia do servidor",
    "56": "conexão resetada",
}


def step_verify(ctx: Context) -> None:
    via = "via Traefik" if ctx.cfg.k8s.ingress_host else "diretamente no pod"
    step(f"verify — checando imagem publicada e rotas {via}")
    script = render(
        VERIFY_SH,
        namespace=ctx.cfg.k8s.namespace,
        deployment=ctx.cfg.k8s.deployment,
        ingress_host=ctx.cfg.k8s.ingress_host,
        image=ctx.image if ctx.tag else "",
        paths=" ".join(ctx.cfg.k8s.verify_paths),
        tries=ctx.cfg.k8s.verify_tries,
        contains=ctx.cfg.k8s.verify_contains,
    )
    res = ctx.sh.remote_script(script, capture=True, check=False, label="verify")
    if ctx.sh.dry_run:
        return

    bad: list[str] = []
    for line in res.out.splitlines():
        m = re.match(r"^::([a-z0-9_]+)::(.*)$", line.strip())
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        if key == "content":
            if value.startswith("ok"):
                ok(value)
            else:
                fail(value)
                bad.append("conteúdo")
            continue
        if key == "diag":
            detail(value)
            continue
        if key == "route":
            path, code, curl_rc, err, tries = (value.split("|", 4) + [""] * 5)[:5]
            suffix = f" (tentativa {tries})" if tries not in ("1", "") else ""
            if curl_rc == "0" and code[:1] in ("2", "3"):
                ok(f"{path} → {code}{suffix}")
            else:
                if curl_rc == "0":
                    motivo = _HTTP_HINTS.get(code, f"HTTP {code}")
                else:
                    motivo = _CURL_ERRORS.get(curl_rc, f"curl exit {curl_rc}")
                fail(f"{path} → {code} — {motivo}{(': ' + err) if err.strip() else ''}")
                bad.append(path)
        elif key == "error":
            fail(value)
            bad.append(value)
        else:
            ok(f"{key}: {value}")

    if res.code != 0 or bad:
        if ctx.args.auto_rollback:
            warn("verificação falhou — executando rollback automático")
            do_rollback(ctx, revision=0)
            raise DeployError("deploy revertido após falha na verificação")
        raise DeployError(
            "verificação falhou. Rode `python deploy/iac.py rollback` para reverter."
        )


def step_prune(ctx: Context) -> None:
    if ctx.args.no_prune:
        step("prune — pulado (--no-prune)")
        return
    step(f"prune — mantendo as {ctx.cfg.image.keep} imagens mais recentes")
    script = render(
        PRUNE_SH,
        repo=ctx.cfg.image.repository,
        ctr_ns=ctx.cfg.image.containerd_ns,
        keep=ctx.cfg.image.keep,
        sudo_setup=sudo_setup(ctx),
    )
    res = ctx.sh.remote_script(
        script, capture=True, check=False, tty=ctx.needs_tty, label="prune"
    )
    for line in res.out.splitlines():
        m = re.match(r"^::([a-z0-9_]+)::(.*)$", line.strip())
        if m:
            ok(f"{m.group(1)}: {m.group(2)}")


PIPELINE: list[tuple[str, Callable[[Context], None]]] = [
    ("preflight", step_preflight),
    ("build", step_build),
    ("package", step_package),
    ("upload", step_upload),
    ("deploy", step_deploy),
    ("verify", step_verify),
    ("prune", step_prune),
]
STEP_NAMES = [name for name, _ in PIPELINE]


# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------


def cmd_deploy(ctx: Context) -> int:
    selected = set(STEP_NAMES)
    if ctx.args.only:
        selected = _parse_steps(ctx.args.only)
    if ctx.args.skip:
        selected -= _parse_steps(ctx.args.skip)
    if ctx.args.from_step:
        idx = STEP_NAMES.index(ctx.args.from_step)
        selected &= set(STEP_NAMES[idx:])

    info(f"pipeline: {' → '.join(n for n in STEP_NAMES if n in selected)}")
    if ctx.sh.dry_run:
        warn("DRY-RUN: nada será executado de fato")

    for name, fn in PIPELINE:
        if name not in selected:
            continue
        try:
            fn(ctx)
        except DeployError:
            if ctx.deployed and ctx.args.auto_rollback and name != "verify":
                warn("falha após o deploy — revertendo")
                do_rollback(ctx, revision=0)
            raise

    if ctx.tag and not ctx.sh.dry_run and "deploy" in selected:
        ctx.state.record(
            {
                "at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                "tag": ctx.tag,
                "image": ctx.image,
                "digest": ctx.digest,
                "namespace": ctx.cfg.k8s.namespace,
                "deployment": ctx.cfg.k8s.deployment,
            }
        )

    print()
    ok(
        f"{C.BOLD}concluído em {time.monotonic() - _T0:.1f}s "
        f"({ctx.sh.ssh_calls} conexões SSH){C.RESET}"
    )
    if ctx.image and "deploy" in selected:
        info(f"imagem publicada: {C.BOLD}{ctx.image}{C.RESET}")
        if ctx.cfg.k8s.ingress_host:
            info(f"site: http://{ctx.cfg.k8s.ingress_host}/")
        else:
            info(
                f"alvo: {ctx.cfg.k8s.namespace}/{ctx.cfg.k8s.deployment} "
                "(sem Ingress/DNS configurado)"
            )
    return 0


def _parse_steps(raw: str) -> set[str]:
    names = {s.strip() for s in raw.split(",") if s.strip()}
    unknown = names - set(STEP_NAMES)
    if unknown:
        raise DeployError(
            f"etapa desconhecida: {', '.join(sorted(unknown))}. "
            f"Válidas: {', '.join(STEP_NAMES)}"
        )
    return names


def do_rollback(ctx: Context, revision: int) -> None:
    script = render(
        ROLLBACK_SH,
        namespace=ctx.cfg.k8s.namespace,
        deployment=ctx.cfg.k8s.deployment,
        revision=revision,
        timeout=ctx.cfg.k8s.rollout_timeout,
    )
    ctx.sh.remote_script(script, tty=ctx.needs_tty, label="rollback")


def cmd_rollback(ctx: Context) -> int:
    step(f"rollback — deployment/{ctx.cfg.k8s.deployment}")
    do_rollback(ctx, revision=ctx.args.to_revision)
    ok("rollback concluído")
    return 0


def cmd_status(ctx: Context) -> int:
    step(f"status — {ctx.cfg.k8s.namespace}/{ctx.cfg.k8s.deployment}")
    script = render(
        STATUS_SH,
        namespace=ctx.cfg.k8s.namespace,
        deployment=ctx.cfg.k8s.deployment,
    )
    ctx.sh.remote_script(script, check=False, label="status")

    if ctx.state.history:
        print()
        info("últimos deploys registrados localmente:")
        for entry in ctx.state.history[-5:]:
            detail(f"{entry['at']}  {entry['tag']}  sha={entry['digest'][:12]}")
    return 0


def cmd_doctor(ctx: Context) -> int:
    step_preflight(ctx)
    print()
    ok("ambiente pronto")
    return 0


def cmd_install_key(ctx: Context) -> int:
    step("install-key — instalando chave pública no servidor")
    candidates = [
        Path.home() / ".ssh" / "id_ed25519.pub",
        Path.home() / ".ssh" / "id_rsa.pub",
    ]
    pub = next((p for p in candidates if p.exists()), None)
    if pub is None:
        info("nenhuma chave encontrada — gerando ed25519")
        ctx.sh.local(
            [
                "ssh-keygen",
                "-t",
                "ed25519",
                "-f",
                str(Path.home() / ".ssh" / "id_ed25519"),
                "-N",
                "",
                "-C",
                f"iac@{_slug(Path.cwd())}",
            ]
        )
        pub = Path.home() / ".ssh" / "id_ed25519.pub"

    key = pub.read_text(encoding="utf-8").strip()
    script = (
        "set -euo pipefail\n"
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh\n"
        "touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys\n"
        f"grep -qxF '{key}' ~/.ssh/authorized_keys || echo '{key}' >> ~/.ssh/authorized_keys\n"
        "echo 'chave instalada'\n"
    )
    ctx.sh.remote_script(script, label="install-key")
    ok(f"chave {pub.name} instalada — próximos deploys não pedem senha")
    return 0


def cmd_setup_sudo(ctx: Context) -> int:
    step("setup-sudo — configurando Docker e containerd do k3s/RKE2")
    mode = "full" if ctx.args.full else "narrow"
    script = render(
        SUDOERS_SH,
        user=ctx.cfg.remote.user,
        mode=mode,
        ctr_ns=ctx.cfg.image.containerd_ns,
    )

    if ctx.sh.uses_password:
        # Já temos a senha: o helper SUDO_ASKPASS resolve, sem interação.
        ctx.sh.remote_script(sudo_setup(ctx) + script, label="setup-sudo")
    else:
        # Duas etapas de propósito: grava o script (stdin ocupado pelo
        # conteúdo) e só então executa com TTY (stdin livre para o teclado).
        remote_path = "/tmp/iac-sudo-setup.sh"
        ctx.sh.remote_binary(f"cat > {remote_path}", script.encode("utf-8"))
        info("digite a senha do servidor quando o sudo pedir (só desta vez)")
        res = ctx.sh.interactive(f"bash {remote_path}; rm -f {remote_path}")
        if res.code != 0:
            raise DeployError(
                "não consegui instalar a regra do sudoers. "
                "Rode `python iac.py doctor` para reavaliar."
            )

    ok("acesso configurado — rode `python iac.py doctor` novamente")
    return 0


DOCKERFILE_RUNTIME = """\
# Modo "artifact": o build do frontend acontece no seu PC e este estágio
# empacota somente o conteúdo estático gerado em dist/.
FROM nginx:1.27-alpine

# Limpar o html da imagem base evita publicar o "Welcome to nginx!" caso o
# artefato esteja incompleto.
RUN rm -f /etc/nginx/conf.d/default.conf && rm -rf /usr/share/nginx/html/*
COPY nginx.conf /etc/nginx/conf.d/app.conf
COPY browser/ /usr/share/nginx/html/

# Falha o build se a config for inválida. Sem isso, um erro de sintaxe só
# aparece como CrashLoopBackOff depois do deploy, e o rollout trava.
RUN nginx -t

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \\
  CMD wget -qO- http://127.0.0.1/healthz >/dev/null || exit 1

EXPOSE 80
"""

NGINX_CONF = """\
server {
  listen 80;
  server_name _;
  server_tokens off;

  root /usr/share/nginx/html;
  index index.html;

  gzip on;
  gzip_vary on;
  gzip_min_length 1024;
  gzip_types text/css application/javascript application/json image/svg+xml text/plain application/xml;

  add_header X-Content-Type-Options nosniff always;
  add_header X-Frame-Options SAMEORIGIN always;
  add_header Referrer-Policy strict-origin-when-cross-origin always;

  location = /healthz {
    access_log off;
    add_header Content-Type text/plain;
    return 200 "ok\\n";
  }

  location /assets/ {
    try_files $uri =404;
    add_header Cache-Control "public, max-age=31536000, immutable" always;
    access_log off;
  }

  location ~* \\.(?:woff2?|ttf|svg|png|jpg|jpeg|webp|avif|ico|txt|xml)$ {
    try_files $uri =404;
    add_header Cache-Control "public, max-age=2592000" always;
    access_log off;
  }

  location / {
    add_header Cache-Control "no-cache" always;
    try_files $uri $uri/ /index.html;
  }
}
"""

DOCKERFILE_SOURCE_TMPL = """\
# Modo "source": o build do frontend acontece DENTRO do
# container, no servidor. Use quando quiser reprodutibilidade total e não se
# importar de gastar CPU/disco do no. Para o dia a dia prefira o modo
# "artifact" (build no seu PC, imagem de ~25 MB).

# ---- Build ----
FROM node:22-alpine AS build
WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY . .
RUN npm run build

# ---- Runtime ----
FROM nginx:1.27-alpine

RUN rm -f /etc/nginx/conf.d/default.conf && rm -rf /usr/share/nginx/html/*
COPY deploy/nginx.conf /etc/nginx/conf.d/app.conf
COPY --from=build /app/@@DIST@@ /usr/share/nginx/html

# Falha o build se a config for inválida (ver Dockerfile.runtime).
RUN nginx -t

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \\
  CMD wget -qO- http://127.0.0.1/healthz >/dev/null || exit 1

EXPOSE 80
"""

IAC_TOML_TMPL = """\
# Configuração do orquestrador de deploy (deploy/iac.py).
# NÃO existem defaults de projeto no script: o que não estiver aqui é
# reclamado na hora, para não haver risco de publicar no app errado.

[remote]
host    = "@@HOST@@"
user    = "@@USER@@"
port    = 22
# identity = "~/.ssh/id_ed25519"
workdir = "/tmp/iac-@@SLUG@@"
# Senha: NÃO coloque aqui — este arquivo vai para o git.
# Use deploy/iac.local.toml (ignorado pelo git) ou IAC_SSH_PASSWORD.
# Melhor ainda: python deploy/iac.py install-key -> zero senha.

[k8s]
namespace       = "@@SLUG@@"
deployment      = "@@SLUG@@"
container       = ""                 # vazio = autodetecta via kubectl
ingress_host    = ""                 # vazio = verifica diretamente no pod
ingress_hosts   = []
rollout_timeout = 180
verify_paths    = ["/"]
verify_tries    = 3
verify_contains = "ORZYON"           # trecho obrigatório na home (vazio desliga)

[build]
mode       = "artifact"              # artifact = build no PC | source = no servidor
command    = ["npm", "run", "build"]
dist       = "dist"
dockerfile = "deploy/Dockerfile.runtime"
nginx_conf = "deploy/nginx.conf"

[image]
repository    = "local/@@SLUG@@"
keep          = 5
containerd_ns = "k8s.io"
"""


IAC_LOCAL_EXAMPLE = """\
# Copie para iac.local.toml e preencha. Este arquivo NÃO vai para o git.
# Só é necessário se você optar por autenticar com senha em vez de chave.
#
# [remote]
# password = "sua-senha-do-servidor"
"""


REQUIRED_BY_COMMAND: dict[str, tuple[tuple[str, str], ...]] = {
    "_remoto": (("remote.host", "endereço do servidor"), ("remote.user", "usuário SSH")),
    "_k8s": (("k8s.namespace", "namespace"), ("k8s.deployment", "nome do Deployment")),
    "_build": (("build.dist", "pasta do build"), ("image.repository", "nome da imagem")),
}

_COMMAND_NEEDS = {
    "deploy": ("_remoto", "_k8s", "_build"),
    "doctor": ("_remoto", "_k8s"),
    "status": ("_remoto", "_k8s"),
    "rollback": ("_remoto", "_k8s"),
    "apply": ("_remoto",),
    "install-key": ("_remoto",),
    "setup-sudo": ("_remoto",),
    "init": (),
}


def validate(cfg: Config, command: str, config_path: str) -> None:
    """Falha cedo se faltar configuração.

    Sem isso, campos ausentes cairiam em defaults e o script publicaria num
    app que não é o seu — o pior tipo de erro, porque termina com sucesso.
    """
    faltando: list[str] = []
    for grupo in _COMMAND_NEEDS.get(command, ()):
        for chave, descricao in REQUIRED_BY_COMMAND[grupo]:
            secao, campo = chave.split(".")
            if not getattr(getattr(cfg, secao), campo):
                faltando.append(f"{chave}  ({descricao})")

    if faltando:
        raise DeployError(
            f"configuração incompleta em {config_path}:\n"
            + "".join(f"       - {f}\n" for f in faltando)
            + "       rode `python deploy/iac.py init` para gerar o arquivo."
        )


def render_manifest(cfg: Config) -> str:
    """Manifesto de bootstrap: Namespace + Deployment + Service (+ Ingress).

    A imagem inicial é um nginx vazio de propósito. No primeiro bootstrap a
    imagem da aplicação ainda não existe no containerd, e um Deployment
    apontando para uma tag inexistente nasce em ImagePullBackOff. O
    `iac.py deploy` troca a imagem no primeiro run.
    """
    ns = cfg.k8s.namespace
    name = cfg.k8s.deployment
    container = cfg.k8s.container or name
    hosts = tuple(
        host for host in (cfg.k8s.ingress_hosts or (cfg.k8s.ingress_host,)) if host
    )

    rules = "\n".join(
        f"""    - host: {h}
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: {name}
                port:
                  number: 80"""
        for h in hosts
    )

    manifest = f"""# Gerado por iac.py — bootstrap de {name}.
# Aplique com:  python deploy/iac.py apply
# Depois:       python deploy/iac.py deploy
---
apiVersion: v1
kind: Namespace
metadata:
  name: {ns}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {name}
  namespace: {ns}
  labels:
    app: {name}
spec:
  replicas: 1
  # Sem isso o Kubernetes guarda 10 ReplicaSets antigos por Deployment.
  revisionHistoryLimit: 3
  selector:
    matchLabels:
      app: {name}
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
  template:
    metadata:
      labels:
        app: {name}
    spec:
      containers:
        - name: {container}
          # Placeholder — o `iac.py deploy` substitui pela imagem real.
          image: nginx:1.27-alpine
          imagePullPolicy: IfNotPresent
          ports:
            - name: http
              containerPort: 80
          # Probe em "/" e não em "/healthz": o nginx placeholder não tem
          # /healthz e o pod nunca ficaria Ready no bootstrap.
          readinessProbe:
            httpGet:
              path: /
              port: http
            initialDelaySeconds: 2
            periodSeconds: 5
            timeoutSeconds: 2
          livenessProbe:
            httpGet:
              path: /
              port: http
            initialDelaySeconds: 10
            periodSeconds: 20
            timeoutSeconds: 3
          resources:
            requests:
              cpu: 10m
              memory: 16Mi
            limits:
              memory: 96Mi
          securityContext:
            allowPrivilegeEscalation: false
---
apiVersion: v1
kind: Service
metadata:
  name: {name}
  namespace: {ns}
  labels:
    app: {name}
spec:
  type: ClusterIP
  selector:
    app: {name}
  ports:
    - name: http
      port: 80
      targetPort: http
      protocol: TCP
"""

    if not hosts:
        return manifest

    return manifest + f"""---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {name}
  namespace: {ns}
  labels:
    app: {name}
spec:
  ingressClassName: traefik
  rules:
{rules}
"""


def cmd_apply(ctx: Context) -> int:
    rel = ctx.args.manifest or "deploy/k8s.yaml"
    path = Path(rel)
    if not path.is_absolute():
        path = ctx.root / path
    if not path.exists():
        raise DeployError(
            f"manifesto não encontrado: {path}\n"
            f"       rode `python deploy/iac.py init` para gerá-lo."
        )

    step(f"apply — {path.name} → kubectl apply")
    data = path.read_bytes()
    # Vai pelo stdin controlado pelo Python: nada de redirecionamento do
    # PowerShell nem de aspas aninhadas no comando remoto.
    ctx.sh.remote_binary("kubectl apply -f -", data, label=path.name)
    ok(f"{len(data)} bytes aplicados")
    return 0


def _slug(root: Path) -> str:
    """Nome do projeto a partir da pasta, normalizado para uso em k8s."""
    bruto = re.sub(r"[^a-z0-9-]+", "-", root.name.lower()).strip("-")
    return bruto or "app"


def render_iac_toml(root: Path, cfg: Config) -> str:
    return render(
        IAC_TOML_TMPL,
        slug=_slug(root),
        host=cfg.remote.host or "fda2:67cd:dd48:1100::100",
        user=cfg.remote.user or "gabrielsousa",
    )


def render_dockerfile_source(cfg: Config, root: Path) -> str:
    return render(DOCKERFILE_SOURCE_TMPL, dist=cfg.build.dist or "dist")


def cmd_init(ctx: Context) -> int:
    step("init — gerando arquivos de apoio")
    targets = {
        ctx.root / "deploy" / "Dockerfile.runtime": DOCKERFILE_RUNTIME,
        ctx.root / "deploy" / "Dockerfile.source": render_dockerfile_source(ctx.cfg, ctx.root),
        ctx.root / "deploy" / "nginx.conf": NGINX_CONF,
        ctx.root / "deploy" / "iac.toml": render_iac_toml(ctx.root, ctx.cfg),
        ctx.root / "deploy" / "iac.local.toml.example": IAC_LOCAL_EXAMPLE,
    }
    for path, content in targets.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not ctx.args.force:
            warn(f"{path.relative_to(ctx.root)} já existe (use --force para sobrescrever)")
            continue
        path.write_text(content, encoding="utf-8", newline="\n")
        ok(f"escrito: {path.relative_to(ctx.root)}")

    if ctx.cfg.k8s.namespace and ctx.cfg.k8s.deployment:
        path = ctx.root / "deploy" / "k8s.yaml"
        if not path.exists() or ctx.args.force:
            path.write_text(render_manifest(ctx.cfg), encoding="utf-8", newline="\n")
            ok("escrito: deploy/k8s.yaml")
    else:
        info("k8s.yaml não gerado — preencha k8s.namespace/deployment e rode init de novo")

    gitignore = ctx.root / ".gitignore"
    if gitignore.exists():
        current = gitignore.read_text(encoding="utf-8")
        missing = [
            m
            for m in ("deploy/.iac-state.json", "deploy/iac.local.toml")
            if m not in current
        ]
        if missing:
            with gitignore.open("a", encoding="utf-8") as fh:
                fh.write("\n# orquestrador de deploy: estado local e segredos\n")
                fh.write("\n".join(missing) + "\n")
            ok("adicionado ao .gitignore: " + ", ".join(missing))
    else:
        warn(".gitignore não encontrado — garanta que iac.local.toml não seja commitado")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent


GLOBAL_DEFAULTS: dict[str, object] = {
    "config": "deploy/iac.toml",
    # Resolve pela localização do próprio script, não pelo diretório atual.
    # Assim `python deploy/iac.py` e `cd deploy && python iac.py` são iguais.
    "root": str(PROJECT_ROOT),
    "verbose": False,
    "dry_run": False,
    "host": None,
    "user": None,
    "namespace": None,
}


def _common_parser() -> argparse.ArgumentParser:
    """Flags aceitas tanto antes quanto depois do subcomando.

    Os defaults são SUPPRESS para que a cópia herdada pelo subparser não
    sobrescreva o que já foi lido no nível global — comportamento padrão
    (e indesejado) do argparse com `parents`.
    """
    c = argparse.ArgumentParser(add_help=False)
    S = argparse.SUPPRESS
    c.add_argument("-c", "--config", default=S, help="caminho do iac.toml")
    c.add_argument("-r", "--root", default=S, help="raiz do projeto frontend")
    c.add_argument("-v", "--verbose", action="store_true", default=S, help="ecoa os comandos")
    c.add_argument("-n", "--dry-run", action="store_true", default=S, help="não executa nada")
    c.add_argument("--host", default=S, help="sobrescreve remote.host")
    c.add_argument("--user", default=S, help="sobrescreve remote.user")
    c.add_argument("--namespace", default=S, help="sobrescreve k8s.namespace")
    return c


def build_parser() -> argparse.ArgumentParser:
    common = _common_parser()
    p = argparse.ArgumentParser(
        parents=[common],
        prog="iac.py",
        description="Orquestrador de deploy frontend → k3s/RKE2/Rancher (sem CI/CD).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Etapas do pipeline: " + " → ".join(STEP_NAMES) + "\n\n"
            "Exemplos:\n"
            "  python iac.py install-key\n"
            "  python iac.py setup-sudo\n"
            "  python iac.py doctor\n"
            "  python iac.py deploy --dry-run\n"
            "  python iac.py deploy --skip-build --force\n"
            "  python iac.py deploy --from upload\n"
            "  python iac.py rollback --to-revision 3\n"
        ),
    )
    p.add_argument("--version", action="version", version=f"iac.py {__version__}")

    sub = p.add_subparsers(dest="command")

    d = sub.add_parser("deploy", parents=[common], help="pipeline completo (padrão)")
    d.add_argument("--only", help="executa só estas etapas (csv)")
    d.add_argument("--skip", help="pula estas etapas (csv)")
    d.add_argument("--from", dest="from_step", choices=STEP_NAMES, help="começa daqui")
    d.add_argument("--skip-build", action="store_true", help="reaproveita o dist atual")
    d.add_argument("--no-prune", action="store_true", help="não limpa imagens antigas")
    d.add_argument("--force", action="store_true", help="publica mesmo sem mudanças")
    d.add_argument(
        "--auto-rollback",
        action="store_true",
        help="reverte automaticamente se a verificação falhar",
    )
    d.add_argument("--tag", help="usa esta tag em vez de timestamp+hash")

    r = sub.add_parser("rollback", parents=[common], help="volta para a revisão anterior")
    r.add_argument("--to-revision", type=int, default=0, help="0 = revisão anterior")

    sub.add_parser("status", parents=[common], help="estado do deployment no cluster")
    sub.add_parser("doctor", parents=[common], help="só roda o preflight")
    sub.add_parser("install-key", parents=[common], help="instala sua chave SSH no servidor")
    ss = sub.add_parser(
        "setup-sudo",
        parents=[common],
        help="configura Docker e containerd no servidor (pede a senha uma vez)",
    )
    ss.add_argument(
        "--full",
        action="store_true",
        help="regra ampla (NOPASSWD: ALL) — use só se a estreita não pegar",
    )

    ap = sub.add_parser(
        "apply", parents=[common], help="aplica um manifesto k8s no cluster"
    )
    ap.add_argument(
        "manifest", nargs="?", help="caminho do YAML (padrão: deploy/k8s.yaml)"
    )

    i = sub.add_parser("init", parents=[common], help="gera Dockerfile.runtime, nginx.conf, iac.toml e k8s.yaml")
    i.add_argument("--force", action="store_true", help="sobrescreve se já existir")

    return p


def apply_overrides(cfg: Config, args: argparse.Namespace) -> Config:
    remote = cfg.remote
    if getattr(args, "host", None):
        remote = replace(remote, host=args.host)
    if getattr(args, "user", None):
        remote = replace(remote, user=args.user)

    k8s = cfg.k8s
    if getattr(args, "namespace", None):
        k8s = replace(k8s, namespace=args.namespace)

    return replace(cfg, remote=remote, k8s=k8s)


COMMANDS: dict[str, Callable[[Context], int]] = {
    "deploy": cmd_deploy,
    "rollback": cmd_rollback,
    "status": cmd_status,
    "doctor": cmd_doctor,
    "install-key": cmd_install_key,
    "setup-sudo": cmd_setup_sudo,
    "apply": cmd_apply,
    "init": cmd_init,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        args = parser.parse_args([*(argv or sys.argv[1:]), "deploy"])

    # defaults: SUPPRESS deixa o atributo ausente quando a flag não foi usada
    for flag, gdefault in GLOBAL_DEFAULTS.items():
        if not hasattr(args, flag):
            setattr(args, flag, gdefault)

    # defaults para flags que só existem em alguns subcomandos
    for flag, default in (
        ("only", None),
        ("skip", None),
        ("from_step", None),
        ("skip_build", False),
        ("no_prune", False),
        ("force", False),
        ("auto_rollback", False),
        ("tag", None),
        ("to_revision", 0),
        ("full", False),
        ("manifest", None),
    ):
        if not hasattr(args, flag):
            setattr(args, flag, default)

    root = Path(args.root).resolve()
    if not root.is_dir():
        fail(f"raiz inválida: {root}")
        return 2

    try:
        cfg = apply_overrides(load_config(root / args.config), args)
        validate(cfg, args.command, args.config)
        sh = Shell(cfg, dry_run=args.dry_run, verbose=args.verbose)
        ctx = Context(
            cfg=cfg,
            sh=sh,
            root=root,
            state=State.load(root / "deploy" / ".iac-state.json"),
            args=args,
        )
        if args.tag:
            ctx.tag = args.tag

        info(
            f"{C.BOLD}iac.py {__version__}{C.RESET} · "
            f"{cfg.ssh_target} · {cfg.k8s.namespace}/{cfg.k8s.deployment} · "
            f"mode={cfg.build.mode} · "
            f"auth={'senha' if sh.uses_password else 'chave/agente'}"
        )
        try:
            return COMMANDS[args.command](ctx)
        finally:
            sh.close()

    except DeployError as exc:
        print()
        fail(str(exc))
        return 1
    except KeyboardInterrupt:
        print()
        fail("interrompido pelo usuário")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
