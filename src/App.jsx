import { useEffect, useState } from 'react';

const Arrow = () => <span aria-hidden="true">↗</span>;

const specialties = [
  {
    index: '01',
    label: 'Plataforma e cloud',
    title: 'Ambientes prontos para cargas de IA',
    text: 'Operação de workloads entre nuvem, Kubernetes e infraestrutura própria, com capacidade, isolamento e disponibilidade como requisitos de arquitetura.',
    tags: ['Kubernetes', 'Docker', 'Rancher', 'AWS/EKS', 'Azure', 'GCP', 'VMware'],
  },
  {
    index: '02',
    label: 'Entrega e ciclo de vida',
    title: 'Automação entre o artefato e a produção',
    text: 'Pipelines controlados para serviços, modelos, prompts e configurações, combinando versionamento, implantação, rollback e infraestrutura como código.',
    tags: ['MLOps', 'LLMOps', 'CI/CD', 'GitOps', 'Terraform', 'Ansible', 'MLflow'],
  },
  {
    index: '03',
    label: 'LLMs e agentes',
    title: 'Base operacional para sistemas generativos',
    text: 'Sustentação de RAG, agentes, integrações via MCP e APIs, com controles de acesso e aprovação humana para ações críticas.',
    tags: ['RAG', 'LangChain', 'LangGraph', 'Embeddings', 'MCP', 'APIs'],
  },
  {
    index: '04',
    label: 'SRE e segurança',
    title: 'Confiabilidade que pode ser observada',
    text: 'Métricas, logs, traces, resposta a incidentes e proteção entre serviços para transformar sinais técnicos em decisões operacionais.',
    tags: ['OpenTelemetry', 'Prometheus', 'Grafana', 'ELK', 'IAM/RBAC', 'mTLS'],
  },
];

const experiences = [
  {
    period: 'ago/2025 — atual',
    role: 'Engenheiro Sênior de Plataforma de IA e SRE',
    company: 'Central IT · Tecnologia em Negócios',
    context: 'Plataformas de IA para contratos governamentais, ministérios, órgãos públicos e projetos internos.',
    bullets: [
      'Operação de Kubernetes/EKS e Docker para aplicações de IA, inferência e agentes.',
      'Entregas automatizadas de serviços, modelos, prompts e configurações com implantação controlada e rollback.',
      'Sustentação de RAG e agentes com MCP, APIs, auditoria e aprovação humana em ações críticas.',
      'Observabilidade, infraestrutura como código, resposta a incidentes e recuperação de serviços.',
    ],
    tags: ['Kubernetes', 'EKS', 'GitOps', 'RAG', 'MCP', 'SRE'],
  },
  {
    period: 'abr/2024 — ago/2025',
    role: 'Engenheiro de Plataforma de IA e Infraestrutura Cloud',
    company: 'Global Hitss Brasil · Cliente CAIXA Econômica Federal',
    context: 'Operação de plataformas e infraestrutura de IA no setor financeiro, entre cloud e on-premises.',
    bullets: [
      'Provisionamento de ambientes, dependências, conectividade, permissões e disponibilidade para aplicações e modelos.',
      'Suporte à implantação de serviços de IA, artefatos versionados, configurações e prompts para aplicações com LLMs.',
      'Automação em Python e PowerShell para rotinas operacionais, acessos e validações de segurança.',
      'Investigação de falhas e aplicação de IAM/RBAC, segredos, criptografia e autenticação entre serviços.',
    ],
    tags: ['AWS', 'Azure', 'Google Cloud', 'Python', 'PowerShell', 'IAM/RBAC'],
  },
  {
    period: 'out/2021 — abr/2024',
    role: 'Engenheiro de Infraestrutura e Plataforma',
    company: 'Hugtak Soluções e Sistemas · Cliente ABR Telecom',
    context: 'Infraestrutura on-premises, serviços corporativos e workloads de IA em telecomunicações.',
    bullets: [
      'Provisionamento de servidores físicos e VMware, com gestão de sistemas, dependências e recursos.',
      'Implantação de aplicações baseadas em modelos e diagnóstico de ambientes.',
      'Automação de provisionamento, backups e acessos com Python, PowerShell e Bash.',
      'Monitoramento de capacidade, continuidade de serviços e diagnóstico de rede.',
    ],
    tags: ['Linux', 'Windows Server', 'VMware', 'Bash', 'Redes', 'Observabilidade'],
  },
];

const education = [
  { status: 'Em andamento', date: 'set/2026 — dez/2027', title: 'MBA em Inteligência Artificial', school: 'Universidade Cruzeiro do Sul' },
  { status: 'Concluída', date: 'abr/2025 — dez/2025', title: 'Pós-graduação em Segurança da Informação', school: 'GRAN' },
  { status: 'Concluído', date: 'jun/2025 — out/2025', title: 'MBA em Gestão de Projetos', school: 'Instituto Faculeste' },
  { status: 'Concluído', date: 'jan/2019 — dez/2022', title: 'Bacharelado em Engenharia de Software', school: 'UNICEPLAC' },
];

function App() {
  const [menuOpen, setMenuOpen] = useState(false);

  useEffect(() => {
    document.body.classList.toggle('menu-open', menuOpen);
    return () => document.body.classList.remove('menu-open');
  }, [menuOpen]);

  useEffect(() => {
    const closeOnEscape = (event) => event.key === 'Escape' && setMenuOpen(false);
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, []);

  return (
    <>
      <a className="skip-link" href="#conteudo">Pular para o conteúdo</a>

      <header className="site-header">
        <a className="personal-mark" href="#inicio" aria-label="Matheus Sousa — início">
          <span className="mark-symbol" aria-hidden="true">MS</span>
          <strong>Matheus Sousa</strong>
        </a>
        <button className="menu-button" type="button" aria-expanded={menuOpen} aria-controls="site-nav" aria-label={menuOpen ? 'Fechar menu' : 'Abrir menu'} onClick={() => setMenuOpen((value) => !value)}>
          <span /><span />
        </button>
        <nav id="site-nav" className={menuOpen ? 'site-nav is-open' : 'site-nav'} aria-label="Navegação principal">
          <a href="#sobre" onClick={() => setMenuOpen(false)}>Perfil</a>
          <a href="#especialidades" onClick={() => setMenuOpen(false)}>Especialidades</a>
          <a href="#experiencia" onClick={() => setMenuOpen(false)}>Experiência</a>
          <a href="#formacao" onClick={() => setMenuOpen(false)}>Formação</a>
          <a className="nav-contact" href="mailto:matheusbarrosr.desousa@gmail.com">Contato <Arrow /></a>
        </nav>
      </header>

      <main id="conteudo">
        <section id="inicio" className="hero section-shell">
          <div className="hero-copy">
            <p className="eyebrow"><span /> Brasília, DF · Plataforma de IA</p>
            <h1>Infraestrutura que faz a IA <em>ficar de pé.</em></h1>
            <p className="hero-lead">Engenheiro Sênior de Plataforma de IA e SRE. Projeto, automatizo e opero ambientes confiáveis para inferência, RAG e agentes — do servidor físico ao Kubernetes.</p>
            <div className="hero-actions">
              <a className="button button-primary" href="#experiencia">Ver experiência <span aria-hidden="true">↓</span></a>
              <a className="button button-ghost" href="mailto:matheusbarrosr.desousa@gmail.com">Vamos conversar <Arrow /></a>
            </div>
          </div>

          <aside className="operations-card" aria-label="Resumo de atuação">
            <div className="terminal-bar"><span /><span /><span /><small>platform.status</small></div>
            <div className="signal-field" aria-hidden="true">
              <div className="signal-core">AI</div>
              <span className="node node-a">K8s</span><span className="node node-b">RAG</span><span className="node node-c">SRE</span><span className="node node-d">LLM</span>
            </div>
            <div className="operations-list">
              <div><span>FOCO</span><strong>Plataforma de IA</strong></div>
              <div><span>AMBIENTES</span><strong>Cloud · On-prem</strong></div>
              <div><span>STATUS</span><strong className="online">● Em produção</strong></div>
            </div>
          </aside>

          <div className="proof-strip" aria-label="Resumo profissional">
            <div><strong>≈5</strong><span>anos em infraestrutura, automação e confiabilidade</span></div>
            <div><strong>3</strong><span>setores críticos: governo, finanças e telecom</span></div>
            <div><strong>01</strong><span>visão integrada de plataforma, segurança e operação</span></div>
          </div>
        </section>

        <section id="sobre" className="profile section-shell section-paper">
          <div className="section-heading">
            <p className="section-kicker">Perfil</p>
            <h2>Da infraestrutura ao comportamento do sistema em produção.</h2>
          </div>
          <div className="profile-grid">
            <div className="profile-copy">
              <p className="profile-lead">Sou Matheus Barros Rodrigues de Sousa, engenheiro de software com atuação em infraestrutura, automação e confiabilidade de plataformas de IA.</p>
              <p>Minha experiência atravessa ambientes físicos, cloud e Kubernetes em operações de telecomunicações, setor financeiro e governo. Trabalho na camada que conecta modelos, aplicações e agentes às condições reais de produção: entrega automatizada, segurança, observabilidade, capacidade e recuperação.</p>
            </div>
            <div className="operating-principles" aria-label="Princípios de trabalho">
              <article><span>01</span><h3>Automatizar com controle</h3><p>Versionamento, validação e rollback fazem parte da entrega.</p></article>
              <article><span>02</span><h3>Observar antes de escalar</h3><p>Métricas, logs e traces orientam capacidade e resposta.</p></article>
              <article><span>03</span><h3>Segurança por desenho</h3><p>Acessos, segredos e identidade entre serviços entram desde o início.</p></article>
            </div>
          </div>
        </section>

        <section id="especialidades" className="specialties section-shell">
          <div className="section-heading section-heading-dark">
            <p className="section-kicker">Especialidades</p>
            <div><h2>Uma plataforma é o conjunto das suas conexões.</h2><p className="section-intro">Cloud, automação, modelos e operação não são trilhas isoladas. O trabalho está em tornar o conjunto previsível, seguro e recuperável.</p></div>
          </div>
          <div className="specialty-list">
            {specialties.map((item) => (
              <article className="specialty-row" key={item.index}>
                <div className="specialty-index"><span>{item.index}</span><small>{item.label}</small></div>
                <h3>{item.title}</h3>
                <p>{item.text}</p>
                <div className="tag-list" aria-label={`Tecnologias de ${item.label}`}>{item.tags.map((tag) => <span key={tag}>{tag}</span>)}</div>
              </article>
            ))}
          </div>
          <div className="language-rail"><span>Linguagens e automação</span><div><strong>Python</strong><strong>Go</strong><strong>Rust</strong><strong>Bash</strong><strong>PowerShell</strong></div></div>
        </section>

        <section id="experiencia" className="experience section-shell section-paper">
          <div className="section-heading">
            <p className="section-kicker">Experiência</p>
            <h2>Operação em contextos onde confiabilidade não é opcional.</h2>
          </div>
          <div className="timeline">
            {experiences.map((job, index) => (
              <article className="timeline-item" key={job.company}>
                <div className="timeline-meta"><span>0{index + 1}</span><time>{job.period}</time></div>
                <div className="timeline-content">
                  <p className="company">{job.company}</p><h3>{job.role}</h3><p className="context">{job.context}</p>
                  <ul>{job.bullets.map((bullet) => <li key={bullet}>{bullet}</li>)}</ul>
                  <div className="tag-list tag-list-light">{job.tags.map((tag) => <span key={tag}>{tag}</span>)}</div>
                </div>
              </article>
            ))}
          </div>
        </section>

        <section id="formacao" className="education section-shell">
          <div className="section-heading section-heading-dark">
            <p className="section-kicker">Formação</p>
            <div><h2>Engenharia, segurança e gestão para sustentar a técnica.</h2><p className="section-intro">Uma formação construída para operar sistemas complexos com visão de arquitetura, risco e entrega.</p></div>
          </div>
          <div className="education-grid">
            {education.map((item, index) => (
              <article key={item.title}><span className="education-number">0{index + 1}</span><div className="education-status"><span>{item.status}</span><time>{item.date}</time></div><h3>{item.title}</h3><p>{item.school}</p></article>
            ))}
          </div>
          <div className="certifications">
            <div><p className="section-kicker">Certificações</p><h3>Fundamentos sólidos para ambientes críticos.</h3></div>
            <ul>
              <li><strong>Segurança em Linux</strong><span>IBSEC · out/2025 — out/2028</span></li>
              <li><strong>Fundamentos em Redes</strong><span>IBSEC · ago/2025 — ago/2028</span></li>
            </ul>
          </div>
        </section>

        <section id="contato" className="contact section-shell">
          <p className="section-kicker">Contato</p>
          <h2>Quer conversar sobre uma plataforma que precisa operar de verdade?</h2>
          <p className="contact-location">Brasília, DF · Brasil</p>
          <div className="contact-actions">
            <a className="button button-primary" href="mailto:matheusbarrosr.desousa@gmail.com">Enviar e-mail <Arrow /></a>
            <a className="button button-ghost" href="https://www.linkedin.com/in/matheus-barros-rodrigues-de-sousa-053589196/" target="_blank" rel="noreferrer">LinkedIn <Arrow /></a>
          </div>
          <footer className="footer-row">
            <p>© {new Date().getFullYear()} Matheus Sousa</p>
            <div><a href="https://github.com/IMatheusBRs" target="_blank" rel="noreferrer">GitHub</a><a href="https://huggingface.co/IMatheusBRs" target="_blank" rel="noreferrer">Hugging Face</a><a href="mailto:matheusbarrosr.desousa@gmail.com">E-mail</a></div>
          </footer>
        </section>
      </main>
    </>
  );
}

export default App;
