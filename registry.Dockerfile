FROM registry:3
ARG VERSION
ENV VERSION=$VERSION
ARG CONFIG_FILE=config.yml
COPY ${CONFIG_FILE} /etc/distribution/config.yml
