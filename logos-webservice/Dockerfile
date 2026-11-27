# Both compiler and runtime use the same verified Temurin security release.
FROM maven:3-eclipse-temurin-27@sha256:3d8de45e72f62c97735db13bbae86445966001d5c05c2c8a137d062c5e8379b2 AS java-runtime
COPY install-temurin.sh /tmp/install-temurin.sh
RUN sh /tmp/install-temurin.sh jre

FROM maven:3-eclipse-temurin-27@sha256:3d8de45e72f62c97735db13bbae86445966001d5c05c2c8a137d062c5e8379b2 AS maven-project
# Official Temurin archives carry the patch before the base images are rebuilt.
COPY install-temurin.sh /tmp/install-temurin.sh
RUN sh /tmp/install-temurin.sh jdk && rm /tmp/install-temurin.sh
ENV JAVA_VERSION=jdk-25.0.5.1+2
# Allow cold CI builds more time to recover from transient Maven Central
# throttling. Resolver honors Retry-After and still fails when retries run out.
ENV MAVEN_ARGS="-Daether.transport.http.retryHandler.count=5"
WORKDIR /build
COPY pom.xml .

RUN --mount=type=cache,target=/root/.m2 mvn dependency:go-offline -q

FROM maven-project AS e2e-jdbc
# Reuse the service's resolved dependencies when building the migration image.
RUN --mount=type=cache,target=/root/.m2 mvn -B -q dependency:copy-dependencies \
    -DincludeScope=runtime -DincludeGroupIds=org.postgresql -DincludeArtifactIds=postgresql -DoutputDirectory=/jdbc

FROM liquibase/liquibase:5.0.4 AS e2e-migrate
COPY --from=e2e-jdbc /jdbc/postgresql-*.jar /liquibase/lib/

FROM maven-project AS builder
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
