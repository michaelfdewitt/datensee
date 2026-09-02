FROM gcr.io/dataflow-templates-base/java21-template-launcher-base:latest

ARG JAR_PATH=build/libs/datensee-pipeline.jar

ENV FLEX_TEMPLATE_JAVA_MAIN_CLASS="com.datensee.DatensEEPipeline"
ENV FLEX_TEMPLATE_JAVA_CLASSPATH="/template/datensee-pipeline.jar"

COPY ${JAR_PATH} /template/datensee-pipeline.jar
