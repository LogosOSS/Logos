package de.tum.cit.aet.logos.logoswebservice.configuration.controller;

import java.util.List;

import org.springframework.http.ResponseEntity;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestAttribute;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import de.tum.cit.aet.logos.logoswebservice.auth.AuthContext;
import de.tum.cit.aet.logos.logoswebservice.configuration.dto.ModelLicenseRequestDTO;
import de.tum.cit.aet.logos.logoswebservice.configuration.dto.ModelLicenseResponse;
import de.tum.cit.aet.logos.logoswebservice.configuration.dto.RemoveModelRequestDTO;
import de.tum.cit.aet.logos.logoswebservice.configuration.dto.RequestedModelResponse;
import de.tum.cit.aet.logos.logoswebservice.configuration.dto.SubmitModelRequestDTO;
import de.tum.cit.aet.logos.logoswebservice.configuration.service.RequestedModelService;
import de.tum.cit.aet.logos.logoswebservice.identity.entity.Role;

/**
 * The model-request voting page (embedded in the Models view): vote for a model
 * Logos does not serve yet, take the vote back, and see what everyone has voted
 * for.
 *
 * It is open to every authenticated role (like the model list), since
 * anyone with an account may want a model that is not on the platform yet. The
 * voter is the authenticated user, so "one vote per user per model" and undo
 * are keyed to them.
 */
@RestController
@RequestMapping("/logosdb")
public class ModelRequestController {

    private final RequestedModelService requestedModelService;

    public ModelRequestController(RequestedModelService requestedModelService) {
        this.requestedModelService = requestedModelService;
    }

    /** The requested models, most-voted first, with the caller's vote state. */
    @PostMapping("/get_model_requests")
    public ResponseEntity<List<RequestedModelResponse>> getModelRequests(
            @RequestAttribute("authContext") AuthContext auth) {
        return ResponseEntity.ok(requestedModelService.list(auth.userId()));
    }

    /**
     * Re-run the Hugging Face precheck for every flagged model and clear the
     * flag from the ones the license for has been accepted, and fetch the
     * license for every readable model that does not carry one yet. The Model
     * requests' refresh button calls this before re-reading the list, so a
     * freshly accepted license unflags the row — and its license becomes
     * visible — without anyone voting again.
     */
    @PostMapping("/recheck_model_requests")
    public ResponseEntity<Void> recheckModelRequests() {
        requestedModelService.recheckGatedModels();
        requestedModelService.fetchMissingLicenses();
        return ResponseEntity.ok().build();
    }

    /** The model's license — state, SPDX id and text — for the acceptance dialog. */
    @PostMapping("/get_model_license")
    public ResponseEntity<ModelLicenseResponse> getModelLicense(@RequestBody ModelLicenseRequestDTO req,
                                                                @RequestAttribute("authContext") AuthContext auth) {
        return ResponseEntity.ok(requestedModelService.license(req.modelId()));
    }

    /**
     * Record the Logos admin's global acceptance of the model's license. Only
     * a model whose license is available can be accepted (400 otherwise);
     * accepting again refreshes who and when.
     */
    @PostMapping("/approve_model_license")
    @PreAuthorize("hasAuthority('" + Role.Names.LOGOS_ADMIN + "')")
    public ResponseEntity<ModelLicenseResponse> approveModelLicense(@RequestBody ModelLicenseRequestDTO req,
                                                                    @RequestAttribute("authContext") AuthContext auth) {
        return ResponseEntity.ok(requestedModelService.approveLicense(req.modelId(), auth.userId()));
    }

    /**
     * Revoke the Logos admin's acceptance of the model's license — and the
     * team acceptances that depend on it. Idempotent.
     */
    @PostMapping("/revoke_model_license")
    @PreAuthorize("hasAuthority('" + Role.Names.LOGOS_ADMIN + "')")
    public ResponseEntity<ModelLicenseResponse> revokeModelLicense(@RequestBody ModelLicenseRequestDTO req) {
        return ResponseEntity.ok(requestedModelService.revokeLicense(req.modelId()));
    }

    /**
     * Cast the caller's vote for a model, naming the team it is wanted for.
     * The service's outcomes are mapped to HTTP in
     * {@link ModelRequestExceptionHandler}: 409 when the model is already
     * served by Logos, 400 when the name is invalid, when no team is given
     * or the caller is not a member of it, or when the Hugging Face precheck
     * does not find the repository, and 503 when the precheck could not be
     * performed (the user should retry). Re-voting an already-voted model is
     * a no-op.
     */
    @PostMapping("/add_model_request")
    public ResponseEntity<RequestedModelResponse> addModelRequest(@RequestBody SubmitModelRequestDTO req,
                                                                  @RequestAttribute("authContext") AuthContext auth) {
        return ResponseEntity.ok(requestedModelService.vote(auth.userId(), req.name(), req.teamId()));
    }

    /** Take the caller's vote back for a model. Idempotent. */
    @PostMapping("/remove_model_request")
    public ResponseEntity<RequestedModelResponse> removeModelRequest(@RequestBody RemoveModelRequestDTO req,
                                                                     @RequestAttribute("authContext") AuthContext auth) {
        return ResponseEntity.ok(requestedModelService.undo(auth.userId(), req.name()));
    }
}
