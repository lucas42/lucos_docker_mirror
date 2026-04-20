FROM registry:2
ARG VERSION
ENV VERSION=$VERSION
ARG CONFIG_FILE=config.yml
COPY ${CONFIG_FILE} /etc/docker/registry/config.yml
