FROM node:22-alpine AS deps
WORKDIR /app
COPY strategy_builder/frontend/package*.json ./
RUN npm ci

FROM node:22-alpine AS builder
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
ENV BACKEND_INTERNAL_URL=http://backend:8000
COPY --from=deps /app/node_modules ./node_modules
COPY strategy_builder/frontend ./
RUN npm run build

FROM node:22-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV BACKEND_INTERNAL_URL=http://backend:8000
COPY --from=builder /app ./
EXPOSE 3000
CMD ["npm", "run", "start"]
