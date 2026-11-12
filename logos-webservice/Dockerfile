FROM maven:3.10-eclipse-temurin-25 AS builder
WORKDIR /build
COPY pom.xml .

RUN --mount=type=cache,target=/root/.m2 mvn dependency:go-offline -q
COPY src ./src
RUN --mount=type=cache,target=/root/.m2 mvn package -DskipTests -q

FROM eclipse-temurin:25-jre
# Mirror the amd64 archives while retaining Ubuntu signature verification.
RUN sed -i -E \
    's#https?://(archive|security)\.ubuntu\.com/ubuntu#https://mirrors.edge.kernel.org/ubuntu#g' \
    /etc/apt/sources.list.d/ubuntu.sources \
  && apt-get update \
  && apt-get install -y --no-install-recommends git openssh-client ca-certificates \
  && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=builder /build/target/*.war app.war
EXPOSE 8081
ENTRYPOINT ["java", "-jar", "app.war"]
