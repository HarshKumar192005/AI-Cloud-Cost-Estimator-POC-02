FROM node:20-alpine
WORKDIR /app

ARG NEXT_PUBLIC_API_URL=http://localhost:8000
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
ENV NODE_ENV=production

COPY package.json ./
RUN npm config set fetch-retries 5 \
    && npm config set fetch-retry-mintimeout 20000 \
    && npm config set fetch-retry-maxtimeout 120000 \
    && npm install

COPY . .
RUN npm run build

EXPOSE 3000
CMD ["npm", "start"]