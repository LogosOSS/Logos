# Both compiler and runtime use the same verified Temurin security release.
FROM maven:3-eclipse-temurin-26@sha256:69a6c37f0a91fca2c9d0cdd684c9c0ea7b1bc00d4196749c24440b2e02ad163d AS java-runtime
COPY install-temurin.sh /tmp/install-temurin.sh
RUN sh /tmp/install-temurin.sh jre

FROM maven:3-eclipse-temurin-26@sha256:69a6c37f0a91fca2c9d0cdd684c9c0ea7b1bc00d4196749c24440b2e02ad163d AS builder
# Official Temurin archives carry the patch before the base images are rebuilt.
COPY install-temurin.sh /tmp/install-temurin.sh
RUN sh /tmp/install-temurin.sh jdk && rm /tmp/install-temurin.sh
ENV JAVA_VERSION=jdk-25.0.5.1+2
WORKDIR /build
COPY pom.xml .

RUN --mount=type=cache,target=/root/.m2 mvn dependency:go-offline -q
COPY src ./src
RUN --mount=type=cache,target=/root/.m2 mvn package -DskipTests -q

FROM eclipse-temurin:25-jre-noble@sha256:f3dd0838e69b881cc382237d5901e74ae3705b1056ad964b2e0e523b174fef99
RUN rm -rf /opt/java/openjdk
COPY --from=java-runtime /opt/java/openjdk /opt/java/openjdk
ENV JAVA_VERSION=jdk-25.0.5.1+2
# apt installs exact versions from a fixed Ubuntu archive snapshot, so every
# build gets the same packages; bump APT_SNAPSHOT, the pins and the base image
# digest together. Snapshot lists are older than their Valid-Until date by
# design, hence the apt setting.
ARG APT_SNAPSHOT=20261117T000000Z
RUN sed -i -E \
      "s#https?://(archive|security)\.ubuntu\.com/ubuntu/?#https://snapshot.ubuntu.com/ubuntu/${APT_SNAPSHOT}/#g" \
      /etc/apt/sources.list.d/ubuntu.sources \
  && ! grep -q -E '(archive|security)\.ubuntu\.com' /etc/apt/sources.list.d/ubuntu.sources \
  && printf 'Acquire::Check-Valid-Until "false";\nAcquire::Retries "3";\n' > /etc/apt/apt.conf.d/99snapshot

RUN apt-get update -o APT::Update::Error-Mode=any \
  && apt-get install -y --no-install-recommends \
       git=1:2.43.0-1ubuntu7.4 openssh-client=1:9.6p1-3ubuntu13.20 \
       ca-certificates=20260816ubuntu0.24.04.2 \
  && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=builder /build/target/*.war app.war
RUN groupadd --system --gid 10001 logos \
  && useradd --uid 10001 --gid logos --create-home --no-log-init logos
ENV HOME=/home/logos
USER 10001:10001
EXPOSE 8081
ENTRYPOINT ["java", "-jar", "app.war"]
