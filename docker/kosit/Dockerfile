# Official KoSIT validator + XRechnung rules, served over HTTP (daemon mode).
FROM eclipse-temurin:21-jre
ARG VALIDATOR_VERSION=1.6.3
ARG CONFIG_RELEASE=v2026-08-31
ARG CONFIG_ZIP=xrechnung-3.0.2-validator-configuration-2026-08-31.zip
WORKDIR /opt/kosit
RUN apt-get update && apt-get install -y --no-install-recommends curl unzip && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL -o validator.jar \
      "https://repo1.maven.org/maven2/org/kosit/validator/${VALIDATOR_VERSION}/validator-${VALIDATOR_VERSION}-standalone.jar" \
 && curl -fsSL -o config.zip \
      "https://github.com/itplr-kosit/validator-configuration-xrechnung/releases/download/${CONFIG_RELEASE}/${CONFIG_ZIP}" \
 && unzip -q config.zip -d config && rm config.zip
EXPOSE 8080
# -Xmx keeps the JVM inside the container memory limit (4 GB VPS)
CMD ["java", "-Xmx700m", "-jar", "validator.jar", "-s", "config/scenarios.xml", "-r", "config", "-D", "-H", "0.0.0.0", "-P", "8080"]
