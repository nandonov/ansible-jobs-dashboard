# This Dockerfile is for the frontend only.
# Ensure you also have a backend Dockerfile and docker-compose.yml correctly referencing both services.

# ---- Build Stage ----
# Use a patched Node 20 on Alpine for reduced CVEs and smaller image
FROM node:20.17.0-alpine3.20 AS build

WORKDIR /app

# Install dependencies
COPY package*.json ./
# Use npm install instead of npm ci to avoid package-lock.json requirement
RUN npm install

# Copy rest of the code
COPY . .

# Build React production bundle
RUN npm run build

# ---- Serve Stage ----
# Bump nginx to a patched 1.27.x on Alpine
FROM nginx:1.27.3-alpine

# Default backend origin (can be overridden at runtime)
ENV BACKEND_ORIGIN=http://backend:8000

# Copy built React files
COPY --from=build /app/dist /usr/share/nginx/html

# Template-based nginx config to allow env substitution at runtime
COPY nginx.conf /etc/nginx/templates/default.conf.template

# Drop privileges to the bundled non-root nginx user
RUN mkdir -p /var/run/nginx \
	&& touch /var/run/nginx.pid \
	&& chown -R nginx:nginx \
		/var/cache/nginx \
		/var/log/nginx \
		/var/run/nginx \
		/var/run/nginx.pid \
		/usr/share/nginx/html \
		/etc/nginx/conf.d \
		/etc/nginx/templates \
	&& apk add --no-cache libcap \
	&& setcap 'cap_net_bind_service=+ep' /usr/sbin/nginx \
	&& apk del libcap
USER nginx

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
	CMD wget --spider -q http://127.0.0.1:80 || exit 1

CMD ["nginx", "-g", "daemon off;"]
