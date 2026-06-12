FROM node:20-alpine AS base
WORKDIR /app
COPY package*.json ./
COPY apps/backend/package*.json ./apps/backend/
RUN npm ci --workspace=apps/backend

FROM base AS builder
WORKDIR /app
COPY . .
RUN npm run build --workspace=apps/backend

FROM node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
COPY --from=builder /app/package*.json ./
COPY --from=builder /app/apps/backend/package*.json ./apps/backend/
COPY --from=builder /app/apps/backend/.medusa ./apps/backend/.medusa
RUN npm ci --omit=dev --workspace=apps/backend

EXPOSE 9000
CMD ["npm", "run", "start", "--workspace=apps/backend"]
