package de.tum.cit.aet.logos.logoswebservice.operations.repository;

public interface ModelBreakdownProjection {
    Integer getModelId();
    String getModelName();
    Boolean getModelDeleted();
    Long getRequestCount();
    Double getAvgQueueSeconds();
    Double getAvgRunSeconds();
    Long getColdStarts();
    Long getWarmStarts();
    Long getErrorCount();
}
