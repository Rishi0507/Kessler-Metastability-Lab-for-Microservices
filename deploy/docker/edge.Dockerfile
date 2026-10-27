# syntax=docker/dockerfile:1.7

FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM nginx:1.30-alpine
COPY deploy/nginx/edge.conf /etc/nginx/nginx.conf
COPY deploy/nginx/l4.conf /etc/nginx/l4.conf
COPY deploy/nginx/edge-http.conf deploy/nginx/edge-server.conf /etc/nginx/
COPY deploy/nginx/upstreams.docker.conf /etc/nginx/upstreams.conf
COPY deploy/nginx/l4-upstreams.docker.conf /etc/nginx/l4-upstreams.conf
COPY deploy/nginx/snippets/ /etc/nginx/snippets/
RUN printf 'listen 8081 proxy_protocol;\n' > /etc/nginx/edge-listen.conf \
 && printf 'listen 8080;\n' > /etc/nginx/l4-listen.conf \
 && printf 'root /usr/share/nginx/html;\n' > /etc/nginx/web-root.conf \
 && mkdir -p /etc/nginx/logs /etc/nginx/cache \
 && ln -sf /dev/stdout /etc/nginx/logs/access.log \
 && ln -sf /dev/stdout /etc/nginx/logs/l4.log \
 && ln -sf /dev/stderr /etc/nginx/logs/error.log \
 && rm -rf /usr/share/nginx/html/*
COPY --from=web /web/dist /usr/share/nginx/html
EXPOSE 8080 8081
