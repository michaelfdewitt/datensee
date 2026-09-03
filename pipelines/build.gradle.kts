plugins {
    java
    id("com.gradleup.shadow") version "8.3.6"
    checkstyle
}

group = "com.datensee"

// Single-sourced from cli/pyproject.toml: the Python client pins itself to
// the Flex Template / GitHub Release for its own version, so the JAR must
// carry the same one. Bump the version in pyproject.toml only.
version = file("../cli/pyproject.toml").readLines()
    .first { it.trim().startsWith("version = ") }
    .substringAfter('"').substringBefore('"')

java {
    sourceCompatibility = JavaVersion.VERSION_25
    targetCompatibility = JavaVersion.VERSION_25
    toolchain {
        languageVersion = JavaLanguageVersion.of(25)
    }
}

tasks.withType<JavaCompile>().configureEach {
    // Dataflow workers run Java 21: emit Java 21 bytecode even when
    // compiling with Java 25. The --release flag restricts the API surface
    // to JDK 21, catching accidental use of newer APIs at compile time.
    options.release.set(21)
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

    // GcpOptions lives here: needed at compile time to install caller-supplied
    // GoogleCredentials onto pipeline options.
    implementation("org.apache.beam:beam-sdks-java-extensions-google-cloud-platform-core:$beamVersion")

    // Dataflow runner: included at runtime; workers resolve this from the JAR
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
        // Beam's vendored protobuf/snappy call restricted native APIs; on
        // JDK 24+ that is a per-launch WARNING unless the JAR grants
        // access here. Ignored by the Java 21 Dataflow workers.
        attributes["Enable-Native-Access"] = "ALL-UNNAMED"
    }
}

checkstyle {
    toolVersion = "10.17.0"
    configFile = file("config/checkstyle/checkstyle.xml")
}
