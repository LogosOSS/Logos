package de.tum.cit.aet.logos.logoswebservice.configuration.controller;

import java.util.Map;

import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import de.tum.cit.aet.logos.logoswebservice.configuration.service.ProviderSpendPolicyService;
import de.tum.cit.aet.logos.logoswebservice.identity.entity.Role;
import de.tum.cit.aet.logos.logoswebservice.audit.AuditLogService;
import de.tum.cit.aet.logos.logoswebservice.gateway.ProviderSpendLedger;

@RestController
@RequestMapping("/logosdb/providers/{providerId}/monthly-spend-cap")
@PreAuthorize("hasAuthority('" + Role.Names.LOGOS_ADMIN + "')")
public class ProviderSpendPolicyController {

    private final ProviderSpendPolicyService service;

    private final ProviderSpendLedger ledger;
    private final AuditLogService audit;

    public ProviderSpendPolicyController(ProviderSpendPolicyService service, ProviderSpendLedger ledger, AuditLogService audit) {
        this.service = service;
        this.ledger = ledger;
        this.audit = audit;
    }

    @GetMapping
    public Map<String, Object> get(@PathVariable int providerId) {
        return service.get(providerId);
    }

    @PutMapping
    public Map<String, Object> schedule(@PathVariable int providerId,
                                        @RequestBody ProviderSpendPolicyService.Change change) {
        return service.schedule(providerId, change);
    }

    @PutMapping("/reviewed-deployments/{modelId}")
    public Map<String, Object> recordReviewedBound(@PathVariable int providerId, @PathVariable int modelId,
            @RequestBody ProviderSpendLedger.BoundReview review) {
        ledger.recordReviewedBound(providerId, modelId, review);
        audit.record("provider.spend_cap_capability_reviewed", "provider", providerId, null, null,
            Map.of("model_id", modelId, "revision", review.revision(), "evidence", review.evidenceUrl()));
        return service.get(providerId);
    }

    @DeleteMapping("/reviewed-deployments/{modelId}")
    public Map<String, Object> removeReviewedBound(@PathVariable int providerId, @PathVariable int modelId,
            @RequestParam String upstreamUrl) {
        ledger.removeReviewedBound(providerId, modelId, upstreamUrl);
        audit.record("provider.spend_cap_capability_removed", "provider", providerId, null, null,
            Map.of("model_id", modelId));
        return service.get(providerId);
    }
}
