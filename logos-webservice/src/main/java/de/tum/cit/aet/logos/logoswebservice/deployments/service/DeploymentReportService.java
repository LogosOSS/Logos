package de.tum.cit.aet.logos.logoswebservice.deployments.service;

import java.time.Clock;
import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.UUID;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.support.TransactionTemplate;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;

import de.tum.cit.aet.logos.logoswebservice.deployments.DeploymentProperties;
import de.tum.cit.aet.logos.logoswebservice.deployments.dto.DeployerApi;
import de.tum.cit.aet.logos.logoswebservice.deployments.entity.Deployer;
import de.tum.cit.aet.logos.logoswebservice.deployments.entity.DeployerKind;
import de.tum.cit.aet.logos.logoswebservice.deployments.entity.DeploymentPlan;
import de.tum.cit.aet.logos.logoswebservice.deployments.entity.DeploymentStep;
import de.tum.cit.aet.logos.logoswebservice.deployments.entity.PlanOrigin;
import de.tum.cit.aet.logos.logoswebservice.deployments.entity.PlanStatus;
import de.tum.cit.aet.logos.logoswebservice.deployments.entity.StepStatus;
import de.tum.cit.aet.logos.logoswebservice.deployments.repository.DeploymentPlanRepository;
import de.tum.cit.aet.logos.logoswebservice.deployments.repository.DeploymentStepRepository;
import jakarta.persistence.EntityManager;

/**
 * What the deployers tell Logos, and what Logos tells them: the orders they ask for and the reports they send.
 *
 * <p>A report carries the whole state of a deployment, so applying it twice gives the same result and a report that
 * was lost is made up by the next one. The first report of an order is also the acknowledgement that stops Logos from
 * handing the order out again. An ended step does not move on, except for one that Logos ended itself (timed out or
 * never started): the host may have run it after all, and the page should show what happened.
 *
 * <p>A deployment that Logos did not order is shown as an observed plan. The deployer is trusted for its own
 * deployments only; the environment and the name of the step come from the deployer record, not from the message.
 */
@Service
public class DeploymentReportService {

    private static final Logger log = LoggerFactory.getLogger(DeploymentReportService.class);

    private static final int MAX_EVENTS = 60;
    private static final int MAX_NOTE_CHARS = 300;
    private static final int MAX_MESSAGE_CHARS = 4000;
    private static final int MAX_LOG_CHARS = 6000;
    private static final int MAX_MODELS = 20;

    private final DeploymentProperties properties;
    private final DeploymentPlanRepository plans;
    private final DeploymentStepRepository steps;
    private final DeployerService deployers;
    private final DeploymentNotifier notifier;
    private final ObjectMapper json;
    private final EntityManager entityManager;
    private final TransactionTemplate transactions;
    private Clock clock = Clock.systemUTC();

    public DeploymentReportService(
            DeploymentProperties properties,
            DeploymentPlanRepository plans,
            DeploymentStepRepository steps,
            DeployerService deployers,
            DeploymentNotifier notifier,
            ObjectMapper json,
            EntityManager entityManager,
            TransactionTemplate transactions) {
        this.properties = properties;
        this.plans = plans;
        this.steps = steps;
        this.deployers = deployers;
        this.notifier = notifier;
        this.json = json;
        this.entityManager = entityManager;
        this.transactions = transactions;
    }

    void setClock(Clock clock) {
        this.clock = clock;
    }

    // ------------------------------------------------------------------ Orders

    /**
     * The steps that are ordered for the deployer and that it has not taken yet. An order that has waited longer than
     * the silence limit is not handed out, also if the sweep has not ended it yet, for example in the first minutes
     * after Logos started again: the deployer would run it at once, and a report cannot call it back. This is the same
     * test as the one of the sweep.
     */
    @Transactional(readOnly = true)
    public List<DeployerApi.Order> openOrders(Deployer deployer) {
        List<DeployerApi.Order> orders = new ArrayList<>();
        Instant cutoff = clock.instant().minus(properties.silenceLimit());
        for (DeploymentStep step : steps.findByDeployerIdAndStatus(deployer.getId(), StepStatus.ordered)) {
            if (step.getLastActivityAt().isBefore(cutoff)) {
                continue;
            }
            plans.findById(step.getPlanId()).ifPresent(plan -> {
                Map<String, String> facts = new LinkedHashMap<>();
                facts.put("id", plan.getId().toString());
                facts.put("environment", plan.getEnvironment());
                facts.put("position", String.valueOf(step.getPosition()));
                facts.put("kind", step.getKind().name());
                facts.put("initiator", plan.getInitiator());
                facts.put("ordered_at", plan.getCreatedAt().toString());
                orders.add(new DeployerApi.Order(step.getId().toString(), plan.getTag(), plan.getInitiator(), facts));
            });
        }
        return orders;
    }

    // ------------------------------------------------------------------ Reports

    /** Apply the reports of a deployer. Returns the ids Logos took: all but those it could not make sense of. */
    @Transactional
    public Set<String> apply(Deployer deployer, DeployerApi.ReportRequest request) {
        Instant now = clock.instant();
        Set<String> acked = new LinkedHashSet<>();
        if (request == null || request.deployments() == null) {
            return acked;
        }
        for (DeployerApi.ReportItem item : request.deployments()) {
            if (item == null || item.id() == null) {
                continue;
            }
            UUID stepId = uuid(item.id());
            if (stepId != null) {
                applyItem(deployer, stepId, item, now);
            }
            // An id that is no UUID is not one of ours. Taking it stops the deployer from sending it for ever.
            acked.add(item.id());
        }
        return acked;
    }

    private void applyItem(Deployer deployer, UUID stepId, DeployerApi.ReportItem item, Instant now) {
        DeploymentStep step = steps.findById(stepId).orElse(null);
        if (step == null) {
            observe(deployer, stepId, item, now);
            return;
        }
        if (!deployer.getId().equals(step.getDeployerId())) {
            log.warn("Deployer {} reported on a step of another deployer; ignored", deployer.getName());
            return;
        }
        DeploymentPlan plan = plans.findByIdForUpdate(step.getPlanId()).orElse(null);
        if (plan == null) {
            return;
        }
        // The plan was locked after the step was read; read the step again so that it is current.
        entityManager.refresh(step);
        List<DeploymentStep> planSteps = steps.findByPlanIdOrderByPositionAscDeployerNameAsc(plan.getId());
        closeIfStale(plan, planSteps, now);
        change(plan, step, planSteps, item, now);
    }

    /**
     * A plan that has been silent for longer than the limit is closed before the report is looked at. Otherwise the
     * first report after a long outage of Logos could release the workers of a plan that was started long ago and has
     * been overtaken since, for example by a fix that an admin deployed by hand.
     */
    private void closeIfStale(DeploymentPlan plan, List<DeploymentStep> planSteps, Instant now) {
        if (plan.getStatus() != PlanStatus.running) {
            return;
        }
        List<DeploymentStep> ended = PlanSequencer.timeOut(
            planSteps, now.minus(properties.silenceLimit()), properties.silenceLimit(), now);
        if (ended.isEmpty()) {
            return;
        }
        settle(plan, planSteps, now);
        steps.saveAll(planSteps);
        ended.forEach(step -> notifyIfLocal(plan, step));
    }

    private void change(
            DeploymentPlan plan,
            DeploymentStep step,
            List<DeploymentStep> planSteps,
            DeployerApi.ReportItem item,
            Instant now) {
        StepStatus before = step.getStatus();
        boolean late = before.isTerminal();
        if (late && before != StepStatus.cancelled && !PlanSequencer.TIMED_OUT.equals(step.getFailure())) {
            return;
        }
        if (item.rejected() != null) {
            if (!late) {
                step.setStatus(StepStatus.failed);
                step.setFailure("rejected");
                step.setMessage(clip("The deployer refused the order: " + item.rejected(), MAX_MESSAGE_CHARS));
                step.setFinishedAt(now);
            }
        } else {
            copy(step, item, now);
        }
        step.setLastActivityAt(now);
        plan.setLastActivityAt(now);
        settle(plan, planSteps, now);
        steps.saveAll(planSteps);
        plans.save(plan);
        if (!late && !before.isTerminal() && step.getStatus().isTerminal()) {
            notifyIfLocal(plan, step);
        }
    }

    private void settle(DeploymentPlan plan, List<DeploymentStep> planSteps, Instant now) {
        if (plan.getOrigin() == PlanOrigin.observed) {
            PlanSequencer.refreshObserved(plan, planSteps, now);
        } else if (plan.getStatus() == PlanStatus.running) {
            PlanSequencer.advance(plan, planSteps, now);
        } else {
            PlanSequencer.refreshAfterLateReport(plan, planSteps, now);
        }
        plans.save(plan);
    }

    /** Put what the deployer reported into the step. A report with an outcome ends the step. */
    private void copy(DeploymentStep step, DeployerApi.ReportItem item, Instant now) {
        boolean reportsEnd = item.outcome() != null && !item.outcome().isBlank();
        StepStatus outcome = reportsEnd
            ? java.util.Objects.requireNonNullElse(StepStatus.fromOutcome(item.outcome()), StepStatus.failed)
            : null;
        step.setPhase(clip(item.phase(), 16));
        step.setFailure(clip(item.failure(), 40));
        step.setMessage(clip(item.message(), MAX_MESSAGE_CHARS));
        step.setRequestedBy(clip(item.requestedBy(), 300));
        Instant started = instant(item.startedAt());
        if (started != null) {
            step.setStartedAt(started);
        }
        step.setEvents(events(item.events()));
        step.setFailureLogs(clip(item.failureLogs(), MAX_LOG_CHARS));
        step.setDetails(details(item));
        if (outcome != null) {
            step.setStatus(outcome);
            Instant finished = instant(item.finishedAt());
            step.setFinishedAt(finished != null ? finished : now);
        } else {
            step.setStatus(StepStatus.running);
            step.setFinishedAt(null);
        }
    }

    /** A deployment that this Logos did not order: make it part of an observed plan. */
    private void observe(Deployer deployer, UUID stepId, DeployerApi.ReportItem item, Instant now) {
        Map<String, Object> facts = item.plan();
        UUID planId = facts == null ? null : uuid(text(facts.get("id")));
        if (planId == null || item.rejected() != null) {
            return;
        }
        String initiator = firstNonBlank(text(facts.get("initiator")), item.requestedBy(), "unknown");
        Instant ordered = instant(text(facts.get("ordered_at")));
        plans.insertObservedIfAbsent(
            planId, deployer.getEnvironment(), clip(item.tag(), 128), clip(initiator, 200), ordered != null ? ordered : now);
        DeploymentPlan plan = plans.findByIdForUpdate(planId).orElse(null);
        if (plan == null
                || plan.getOrigin() != PlanOrigin.observed
                || !plan.getEnvironment().equalsIgnoreCase(deployer.getEnvironment())) {
            // The id belongs to a plan of this Logos or of another environment: a deployer may not add to it.
            return;
        }
        DeploymentStep step = new DeploymentStep();
        step.setId(stepId);
        step.setPlanId(planId);
        step.setPosition(position(facts, deployer));
        step.setKind(deployer.getKind());
        step.setDeployerId(deployer.getId());
        step.setDeployerName(deployer.getName());
        step.setStatus(StepStatus.ordered);
        step.setLastActivityAt(now);
        List<DeploymentStep> planSteps = new ArrayList<>(steps.findByPlanIdOrderByPositionAscDeployerNameAsc(planId));
        planSteps.add(step);
        copy(step, item, now);
        plan.setLastActivityAt(now);
        settle(plan, planSteps, now);
        steps.saveAll(planSteps);
    }

    // ------------------------------------------------------------------ Silence

    /** End the steps that have been silent for longer than the limit, plan by plan. Returns how many it ended. */
    public int closeSilentSteps() {
        Instant now = clock.instant();
        Instant cutoff = now.minus(properties.silenceLimit());
        List<UUID> silentPlans = steps
            .findByStatusInAndLastActivityAtBefore(List.of(StepStatus.ordered, StepStatus.running), cutoff)
            .stream()
            .map(DeploymentStep::getPlanId)
            .distinct()
            .toList();
        int ended = 0;
        for (UUID planId : silentPlans) {
            // One transaction for each plan, so that one plan that cannot be changed does not hold up the others.
            Integer count = transactions.execute(status -> closeSilentStepsOf(planId));
            ended += Objects.requireNonNullElse(count, 0);
        }
        return ended;
    }

    private int closeSilentStepsOf(UUID planId) {
        Instant now = clock.instant();
        DeploymentPlan plan = plans.findByIdForUpdate(planId).orElse(null);
        if (plan == null) {
            return 0;
        }
        List<DeploymentStep> planSteps = steps.findByPlanIdOrderByPositionAscDeployerNameAsc(planId);
        planSteps.forEach(entityManager::refresh);
        List<DeploymentStep> ended = PlanSequencer.timeOut(
            planSteps, now.minus(properties.silenceLimit()), properties.silenceLimit(), now);
        if (ended.isEmpty()) {
            return 0;
        }
        settle(plan, planSteps, now);
        steps.saveAll(planSteps);
        ended.forEach(step -> notifyIfLocal(plan, step));
        return ended.size();
    }

    // ------------------------------------------------------------------ Helpers

    private void notifyIfLocal(DeploymentPlan plan, DeploymentStep step) {
        if (plan.getOrigin() == PlanOrigin.local) {
            notifier.stepEnded(plan, step);
        }
    }

    private String events(List<Map<String, Object>> raw) {
        List<Map<String, String>> clean = new ArrayList<>();
        if (raw != null) {
            int from = Math.max(0, raw.size() - MAX_EVENTS);
            for (Map<String, Object> event : raw.subList(from, raw.size())) {
                if (event == null) {
                    continue;
                }
                Map<String, String> entry = new LinkedHashMap<>();
                entry.put("at", clip(text(event.get("at")), 40));
                entry.put("phase", clip(text(event.get("phase")), 16));
                entry.put("note", clip(text(event.get("note")), MAX_NOTE_CHARS));
                clean.add(entry);
            }
        }
        return write(clean, "[]");
    }

    private String details(DeployerApi.ReportItem item) {
        Map<String, Object> facts = new LinkedHashMap<>();
        if (item.drainedRequests() != null) {
            facts.put("drained_requests", item.drainedRequests());
        }
        if (item.drainSeconds() != null) {
            facts.put("drain_seconds", item.drainSeconds());
        }
        if (item.soleReplicaModels() != null && !item.soleReplicaModels().isEmpty()) {
            facts.put("sole_replica_models", item.soleReplicaModels().stream()
                .limit(MAX_MODELS).map(model -> clip(model, 100)).toList());
        }
        return facts.isEmpty() ? null : write(facts, null);
    }

    private String write(Object value, String fallback) {
        try {
            return json.writeValueAsString(value);
        } catch (JsonProcessingException e) {
            return fallback;
        }
    }

    private static int position(Map<String, Object> facts, Deployer deployer) {
        try {
            return Math.max(0, Integer.parseInt(text(facts.get("position"))));
        } catch (NumberFormatException e) {
            return deployer.getKind() == DeployerKind.core ? 0 : 1;
        }
    }

    private static UUID uuid(String value) {
        try {
            return value == null ? null : UUID.fromString(value);
        } catch (IllegalArgumentException e) {
            return null;
        }
    }

    private static Instant instant(String value) {
        try {
            return value == null || value.isBlank() ? null : Instant.parse(value);
        } catch (RuntimeException e) {
            try {
                return java.time.OffsetDateTime.parse(value).toInstant();
            } catch (RuntimeException ignored) {
                return null;
            }
        }
    }

    private static String text(Object value) {
        return value == null ? null : value.toString();
    }

    private static String clip(String value, int max) {
        if (value == null) {
            return null;
        }
        return value.length() <= max ? value : value.substring(0, max);
    }

    private static String firstNonBlank(String... values) {
        for (String value : values) {
            if (value != null && !value.isBlank()) {
                return value.strip();
            }
        }
        return "";
    }
}
