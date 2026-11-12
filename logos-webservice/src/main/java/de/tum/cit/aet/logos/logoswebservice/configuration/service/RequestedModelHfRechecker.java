package de.tum.cit.aet.logos.logoswebservice.configuration.service;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import de.tum.cit.aet.logos.logoswebservice.configuration.entity.RequestedModel;
import de.tum.cit.aet.logos.logoswebservice.configuration.repository.RequestedModelRepository;
import de.tum.cit.aet.logos.logoswebservice.orchestrator.OrchestratorHfReachabilityClient;
import de.tum.cit.aet.logos.logoswebservice.orchestrator.OrchestratorHfReachabilityClient.HfReachabilityResult;

/**
 * Keeps the Hugging Face state of the registry rows current.
 *
 * <p>Runs at application start and then hourly — on its own single-thread
 * scheduler, so the blocking remote scans never hold up the other scheduled
 * jobs, and at most one pass is in flight — in three passes:
 *
 * <ul>
 *   <li>rows whose {@code is_gated} is still null (they predate the precheck
 *       or their check timed out) are re-checked and get a decisive verdict —
 *       gated when the Hub rejects the repository with the model-gated reason,
 *       not gated otherwise — and a row that lands on not-gated gets its
 *       license fetched with the verdict;</li>
 *   <li>rows flagged as gated are re-checked and unflagged when the central
 *       HF_TOKEN can see the repository again — the license agreement was
 *       accepted, so the flag must not outlive the gating — and the
 *       unflagged row's license is fetched for the first time;</li>
 *   <li>rows the token can read but that carry no license yet (requested
 *       before the license feature, a failed fetch, or unflagged since) get
 *       the license fetched — the backfill.</li>
 * </ul>
 *
 * Only decisive answers are written; {@code unknown} (Hub timeout) leaves the
 * row as it is, so the next pass — or a later vote, which runs the precheck
 * itself — gets another try. The writes are conditional, so a concurrent
 * vote, which stores its own fresh verdict, can never be clobbered by a
 * stale recheck.
 */
@Component
public class RequestedModelHfRechecker {

    private static final Logger log = LoggerFactory.getLogger(RequestedModelHfRechecker.class);

    private final RequestedModelRepository requestedModelRepository;
    private final OrchestratorHfReachabilityClient hfReachabilityClient;
    private final RequestedModelService requestedModelService;

    public RequestedModelHfRechecker(RequestedModelRepository requestedModelRepository,
                                     OrchestratorHfReachabilityClient hfReachabilityClient,
                                     RequestedModelService requestedModelService) {
        this.requestedModelRepository = requestedModelRepository;
        this.hfReachabilityClient = hfReachabilityClient;
        this.requestedModelService = requestedModelService;
    }

    /**
     * One full pass: the unverified rows get a verdict, the flagged rows are
     * cleared where the license was accepted in the meantime, and the rows
     * without a license yet get one fetched. The prechecks are remote round
     * trips, but they only cover the (small) set of pending, flagged and
     * unlicensed rows, at most once an hour.
     */
    @Scheduled(initialDelay = 0, fixedDelay = 3_600_000, scheduler = "hfRecheckScheduler")
    public void recheck() {
        recheckUnverified();
        int cleared = requestedModelService.recheckGatedModels();
        if (cleared > 0) {
            log.info("Hugging Face recheck: {} flagged model(s) unflagged after license acceptance", cleared);
        }
        int licensed = requestedModelService.fetchMissingLicenses();
        if (licensed > 0) {
            log.info("Hugging Face recheck: {} missing license(s) fetched", licensed);
        }
    }

    /**
     * Check every registry row that has no verdict yet and record the decisive
     * ones. Skips quickly (no log) when nothing is pending; a Hub timeout
     * ({@code unknown}) leaves the row empty for the next pass.
     */
    public void recheckUnverified() {
        var unverified = requestedModelRepository.findByIsGatedIsNull();
        if (unverified.isEmpty()) {
            return;
        }
        int recorded = 0;
        int stillUnknown = 0;
        for (RequestedModel model : unverified) {
            HfReachabilityResult result = hfReachabilityClient.check(model.getName());
            if (result == null
                || HfReachabilityResult.STATUS_UNKNOWN.equals(result.status())) {
                stillUnknown++;
                continue;
            }
            if (requestedModelService.recordGatedVerdictIfAbsent(model.getId(), isGated(result)) > 0) {
                recorded++;
                if (!Boolean.TRUE.equals(isGated(result))) {
                    // The token can read the repository, so its license is
                    // readable for the first time — fetch it (best effort).
                    requestedModelService.fetchAndStoreLicense(model);
                }
            }
        }
        log.info("Hugging Face recheck: {} unverified model(s) checked, {} verdict(s) recorded, {} still unknown",
            unverified.size(), recorded, stillUnknown);
    }

    /**
     * The gating verdict of a decisive precheck result: true only when the Hub
     * rejects the repository with the model-gated reason; false when it is
     * reachable or rejected for any other reason.
     */
    private Boolean isGated(HfReachabilityResult result) {
        return !HfReachabilityResult.STATUS_REACHABLE.equals(result.status())
            && HfReachabilityResult.REASON_MODEL_GATED.equals(result.reasonCode());
    }
}
