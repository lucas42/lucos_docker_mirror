FROM registry:3
ARG VERSION
ENV VERSION=$VERSION
COPY config.yml /etc/docker/registry/config.yml
