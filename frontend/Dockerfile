# Фронтенд RecAstra: Next.js в режиме standalone — в итоговом образе только
# server.js и нужные ему файлы, без полного node_modules.
#
# Адрес API вшивается в сборку (переменная NEXT_PUBLIC_*), поэтому задаётся
# аргументом сборки, а не при запуске:
#   docker build --build-arg NEXT_PUBLIC_API_URL=http://localhost:8000 -t recastra-frontend ./frontend
#
# Обычно запускается через docker-compose.yml в корне репозитория.

FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

FROM node:22-alpine AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# Адрес, по которому БРАУЗЕР пользователя достучится до бэкенда.
# Пусто — сборка в деморежиме, без сервера.
ARG NEXT_PUBLIC_API_URL=http://localhost:8000
ENV NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL} \
    NEXT_TELEMETRY_DISABLED=1 \
    NEXT_STANDALONE=1
RUN npm run build

FROM node:22-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0
# Статику standalone-сервер не копирует сам — кладём рядом с server.js.
COPY --from=build --chown=node:node /app/public ./public
COPY --from=build --chown=node:node /app/.next/standalone ./
COPY --from=build --chown=node:node /app/.next/static ./.next/static
USER node
EXPOSE 3000
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:3000/ || exit 1
CMD ["node", "server.js"]
