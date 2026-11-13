FROM maven:3.10-eclipse-temurin-25 AS builder
WORKDIR /build
COPY pom.xml .

RUN --mount=type=cache,target=/root/.m2 mvn dependency:go-offline -q
COPY src ./src
RUN --mount=type=cache,target=/root/.m2 mvn package -DskipTests -q

FROM eclipse-temurin:25-jre
# Keep the base image's signed repositories as a fallback when the mirror is unavailable.
RUN cp /etc/apt/sources.list.d/ubuntu.sources /tmp/ubuntu.sources \
  && sed -i -E \
    's#https?://(archive|security)\.ubuntu\.com/ubuntu#https://mirrors.edge.kernel.org/ubuntu#g' \
    /etc/apt/sources.list.d/ubuntu.sources \
  && install_runtime_packages() { \
    apt-get update -o APT::Update::Error-Mode=any \
      && apt-get install -y --no-install-recommends git openssh-client ca-certificates; \
  } \
  && (install_runtime_packages || { \
    cp /tmp/ubuntu.sources /etc/apt/sources.list.d/ubuntu.sources; \
    install_runtime_packages; \
  }) \
  && rm -rf /var/lib/apt/lists/* /tmp/ubuntu.sources
WORKDIR /app
COPY --from=builder /build/target/*.war app.war
RUN groupadd --system --gid 10001 logos \
  && useradd --uid 10001 --gid logos --create-home --no-log-init logos
ENV HOME=/home/logos
USER 10001:10001
EXPOSE 8081
ENTRYPOINT ["java", "-jar", "app.war"]
