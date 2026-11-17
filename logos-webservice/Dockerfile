FROM maven:3.10-eclipse-temurin-25 AS builder
WORKDIR /build
COPY pom.xml .

RUN --mount=type=cache,target=/root/.m2 mvn dependency:go-offline -q
COPY src ./src
RUN --mount=type=cache,target=/root/.m2 mvn package -DskipTests -q

FROM eclipse-temurin:25-jre-noble@sha256:e3b6f7887a50f20d8f07abf279d8a81a5953fb398a9555bd9edd56b9bd15968c
# apt installs exact versions from a fixed Ubuntu archive snapshot, so every
# build gets the same packages; bump APT_SNAPSHOT, the pins and the base image
# digest together. Snapshot lists are older than their Valid-Until date by
# design, hence the apt setting.
ARG APT_SNAPSHOT=20261117T000000Z
RUN sed -i -E \
      "s#https?://(archive|security)\.ubuntu\.com/ubuntu/?#http://snapshot.ubuntu.com/ubuntu/${APT_SNAPSHOT}/#g" \
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
