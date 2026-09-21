# Modo "source": o build do React/Vite acontece DENTRO do
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
COPY --from=build /app/dist /usr/share/nginx/html

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD wget -qO- http://127.0.0.1/healthz >/dev/null || exit 1

EXPOSE 80
