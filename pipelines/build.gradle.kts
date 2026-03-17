plugins {
    java
    id("com.gradleup.shadow") version "8.3.6"
    checkstyle
}

group = "com.datensee"
version = "0.1.0"

java {
    sourceCompatibility = JavaVersion.VERSION_25
    targetCompatibility = JavaVersion.VERSION_25
    toolchain {
        languageVersion = JavaLanguageVersion.of(25)
    }
}

repositories {
    mavenCentral()
}

val beamVersion = "2.61.0"
val jacksonVersion = "2.17.2"

dependencies {
    // Apache Beam core
    implementation("org.apache.beam:beam-sdks-java-core:$beamVersion")
    implementation("org.apache.beam:beam-runners-direct-java:$beamVersion")

    // Dataflow runner — included at runtime; workers resolve this from the JAR
    runtimeOnly("org.apache.beam:beam-runners-google-cloud-dataflow-java:$beamVersion")

    // GCS output
    implementation("com.google.cloud:google-cloud-storage:2.40.1")

    // Auth
    implementation("com.google.auth:google-auth-library-oauth2-http:1.23.0")

    // Config parsing
    implementation("com.fasterxml.jackson.core:jackson-databind:$jacksonVersion")
    implementation("com.fasterxml.jackson.datatype:jackson-datatype-jsr310:$jacksonVersion")

    // Logging
    implementation("org.slf4j:slf4j-api:2.0.13")
    runtimeOnly("org.slf4j:slf4j-simple:2.0.13")

    // Testing
    testImplementation("org.junit.jupiter:junit-jupiter:5.10.3")
    testImplementation("org.apache.beam:beam-sdks-java-core:$beamVersion") {
        artifact {
            classifier = "tests"
        }
    }
    testRuntimeOnly("org.junit.platform:junit-platform-launcher")
}

tasks.test {
    useJUnitPlatform()
}

tasks.shadowJar {
    archiveBaseName.set("datensee-pipeline")
    archiveClassifier.set("")
    archiveVersion.set("")
    isZip64 = true
    mergeServiceFiles()
    manifest {
        attributes["Main-Class"] = "com.datensee.DatensEEPipeline"
    }
}

checkstyle {
    toolVersion = "10.17.0"
    configFile = file("config/checkstyle/checkstyle.xml")
}
