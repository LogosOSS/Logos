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
import de.tum.cit.aet.logos.logoswebservice.gateway.ProviderSpendLedger;

@RestController
@RequestMapping("/logosdb/providers/{providerId}/monthly-spend-cap")
@PreAuthorize("hasAuthority('" + Role.Names.LOGOS_ADMIN + "')")
public class ProviderSpendPolicyController {

    private final ProviderSpendPolicyService service;

    public ProviderSpendPolicyController(ProviderSpendPolicyService service) {
        this.service = service;
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
        return service.recordReviewedBound(providerId, modelId, review);
    }

    @DeleteMapping("/reviewed-deployments/{modelId}")
    public Map<String, Object> removeReviewedBound(@PathVariable int providerId, @PathVariable int modelId,
            @RequestParam("upstream_url") String upstreamUrl) {
        return service.removeReviewedBound(providerId, modelId, upstreamUrl);
    }
}
