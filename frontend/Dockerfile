FROM node:22.17.1-alpine3.22 AS build
WORKDIR /app
COPY package.json package-lock.json tsconfig.json index.html ./
COPY src ./src
COPY public ./public
RUN npm ci --ignore-scripts && npm run build
FROM nginx:1.29.0-alpine3.22
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY nginx-main.conf /etc/nginx/nginx.conf
RUN chown -R nginx:nginx /var/cache/nginx /var/run /etc/nginx/conf.d && touch /var/run/nginx.pid && chown nginx:nginx /var/run/nginx.pid
USER nginx
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz
CMD ["nginx", "-g", "daemon off;"]
