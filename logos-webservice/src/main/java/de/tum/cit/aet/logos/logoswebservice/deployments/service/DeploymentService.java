package de.tum.cit.aet.logos.logoswebservice.deployments.service;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.UUID;
import java.util.stream.Collectors;

import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import de.tum.cit.aet.logos.logoswebservice.auth.AuthContext;
import de.tum.cit.aet.logos.logoswebservice.common.ConflictException;
import de.tum.cit.aet.logos.logoswebservice.common.NotFoundException;
import de.tum.cit.aet.logos.logoswebservice.deployments.DeploymentProperties;
import de.tum.cit.aet.logos.logoswebservice.deployments.dto.OverviewDTO;
import de.tum.cit.aet.logos.logoswebservice.deployments.dto.PlanDTO;
import de.tum.cit.aet.logos.logoswebservice.deployments.entity.Deployer;
import de.tum.cit.aet.logos.logoswebservice.deployments.entity.DeployerKind;
import de.tum.cit.aet.logos.logoswebservice.deployments.entity.DeploymentPlan;
import de.tum.cit.aet.logos.logoswebservice.deployments.entity.DeploymentStep;
import de.tum.cit.aet.logos.logoswebservice.deployments.entity.PlanOrigin;
import de.tum.cit.aet.logos.logoswebservice.deployments.entity.PlanStatus;
import de.tum.cit.aet.logos.logoswebservice.deployments.entity.StepStatus;
import de.tum.cit.aet.logos.logoswebservice.deployments.repository.DeploymentPlanRepository;
import de.tum.cit.aet.logos.logoswebservice.deployments.repository.DeploymentStepRepository;
import de.tum.cit.aet.logos.logoswebservice.identity.entity.User;
import de.tum.cit.aet.logos.logoswebservice.identity.repository.UserRepository;

/**
 * What an admin does with deployments: look at the environments, start a plan, look at the history, abandon a plan.
 *
 * <p>A plan is the core first, then all workers (see {@link PlanSequencer}). Only one plan that this Logos started may
 * run for an environment: a second start is refused, and a plan that is stuck ends by the silence limit or by an
 * admin who abandons it.
 */
@Service
public class DeploymentService {

    private static final int MAX_LIST = 50;
    private static final int MAX_SUGGESTED_TAGS = 12;

    private final DeploymentProperties properties;
    private final TagPolicy tagPolicy;
    private final DeploymentPlanRepository plans;
    private final DeploymentStepRepository steps;
    private final DeployerService deployers;
    private final UserRepository users;
    private final DeploymentViews views;
    private Clock clock = Clock.systemUTC();

    public DeploymentService(
            DeploymentProperties properties,
            TagPolicy tagPolicy,
            DeploymentPlanRepository plans,
            DeploymentStepRepository steps,
            DeployerService deployers,
            UserRepository users,
            DeploymentViews views) {
        this.properties = properties;
        this.tagPolicy = tagPolicy;
        this.plans = plans;
        this.steps = steps;
        this.deployers = deployers;
        this.users = users;
        this.views = views;
    }

    void setClock(Clock clock) {
        this.clock = clock;
    }

    public boolean enabled() {
        return properties.enabled();
    }

    // ------------------------------------------------------------------ Looking

    @Transactional(readOnly = true)
    public OverviewDTO overview() {
        if (!properties.enabled()) {
            return new OverviewDTO(false, "", List.of(), List.of());
        }
        Instant now = clock.instant();
        List<OverviewDTO.Target> targets = new ArrayList<>();
        for (String environment : environmentsOwnFirst()) {
            List<Deployer> environmentDeployers = deployers.listFor(environment);
            Optional<DeploymentPlan> running = plans.findFirstByEnvironmentIgnoreCaseAndOriginAndStatus(
                environment, PlanOrigin.local, PlanStatus.running);
            String blocked = blockedReason(environment, environmentDeployers, running.isPresent(), now);
            targets.add(new OverviewDTO.Target(
                environment,
                environment.equalsIgnoreCase(properties.environment()),
                properties.isProtected(environment),
                environmentDeployers.stream().map(deployer -> views.deployer(deployer, now)).toList(),
                running.map(DeploymentPlan::getId).orElse(null),
                blocked == null,
                blocked));
        }
        return new OverviewDTO(true, properties.environment(), targets, suggestedTags());
    }

    @Transactional(readOnly = true)
    public List<PlanDTO> plans(String environment, int limit) {
        List<DeploymentPlan> found = environment == null || environment.isBlank()
            ? plans.findTop50ByOrderByCreatedAtDesc()
            : plans.findTop50ByEnvironmentIgnoreCaseOrderByCreatedAtDesc(environment.strip());
        List<DeploymentPlan> limited = found.subList(0, Math.min(found.size(), Math.max(1, Math.min(limit, MAX_LIST))));
        Map<UUID, List<DeploymentStep>> stepsByPlan = steps
            .findByPlanIdIn(limited.stream().map(DeploymentPlan::getId).toList())
            .stream()
            .collect(Collectors.groupingBy(DeploymentStep::getPlanId));
        return limited.stream()
            .map(plan -> views.brief(plan, stepsByPlan.getOrDefault(plan.getId(), List.of())))
            .toList();
    }

    @Transactional(readOnly = true)
    public PlanDTO plan(UUID id) {
        DeploymentPlan plan = plans.findById(id).orElseThrow(() -> new NotFoundException("There is no such deployment."));
        return views.detail(plan, steps.findByPlanIdOrderByPositionAscDeployerNameAsc(id));
    }

    // ------------------------------------------------------------------ Starting and abandoning

    /**
     * Start a deployment of an environment. The core deployer has to be online: the core goes first, and an order
     * that nobody takes would only wait. An offline worker does not block the start; its step ends by the silence limit.
     */
    @Transactional
    public PlanDTO start(String environment, String tag, String confirmation, AuthContext auth) {
        String env = properties.orderable(environment);
        if (env == null) {
            throw new IllegalArgumentException("This Logos does not deploy the environment '" + environment + "'.");
        }
        tagPolicy.check(env, tag);
        if (properties.isProtected(env) && (confirmation == null || !confirmation.strip().equalsIgnoreCase(env))) {
            throw new IllegalArgumentException("Type the name of the environment (" + env + ") to confirm.");
        }
        Instant now = clock.instant();
        List<Deployer> environmentDeployers = deployers.listFor(env);
        Optional<DeploymentPlan> running = plans.findFirstByEnvironmentIgnoreCaseAndOriginAndStatus(
            env, PlanOrigin.local, PlanStatus.running);
        String blocked = blockedReason(env, environmentDeployers, running.isPresent(), now);
        if (blocked != null) {
            throw new ConflictException(blocked);
        }

        DeploymentPlan plan = new DeploymentPlan();
        plan.setId(UUID.randomUUID());
        plan.setEnvironment(env);
        plan.setTag(tag);
        plan.setStatus(PlanStatus.running);
        plan.setOrigin(PlanOrigin.local);
        plan.setInitiator(initiatorName(auth));
        plan.setInitiatorUserId(auth == null ? null : auth.userId());
        plan.setCreatedAt(now);
        plan.setLastActivityAt(now);

        boolean hasCore = environmentDeployers.stream().anyMatch(deployer -> deployer.getKind() == DeployerKind.core);
        List<DeploymentStep> planSteps = new ArrayList<>();
        int position = 0;
        for (Deployer deployer : environmentDeployers) {
            DeploymentStep step = new DeploymentStep();
            step.setId(UUID.randomUUID());
            step.setPlanId(plan.getId());
            step.setPosition(deployer.getKind() == DeployerKind.core ? 0 : ++position);
            step.setKind(deployer.getKind());
            step.setDeployerId(deployer.getId());
            step.setDeployerName(deployer.getName());
            step.setStatus(deployer.getKind() == DeployerKind.core || !hasCore ? StepStatus.ordered : StepStatus.waiting);
            step.setLastActivityAt(now);
            planSteps.add(step);
        }
        try {
            plans.saveAndFlush(plan);
        } catch (DataIntegrityViolationException e) {
            // Another copy of the webservice started a plan for the environment at the same moment.
            throw new ConflictException("A deployment of " + env + " is running.");
        }
        steps.saveAll(planSteps);
        return views.detail(plan, planSteps);
    }

    /**
     * Close a plan that an admin wants to end. Steps that did not start never will. A step that runs on a host goes on
     * there, and so does one whose order a deployer fetched already: Logos cannot take an order back.
     */
    @Transactional
    public PlanDTO abandon(UUID id, AuthContext auth) {
        DeploymentPlan plan = plans.findByIdForUpdate(id).orElseThrow(() -> new NotFoundException("There is no such deployment."));
        if (plan.getOrigin() != PlanOrigin.local) {
            throw new ConflictException("Another Logos started this deployment. Abandon it there.");
        }
        if (plan.getStatus() != PlanStatus.running) {
            throw new ConflictException("The deployment has ended already.");
        }
        List<DeploymentStep> planSteps = steps.findByPlanIdOrderByPositionAscDeployerNameAsc(id);
        PlanSequencer.abandon(plan, planSteps, initiatorName(auth), clock.instant());
        steps.saveAll(planSteps);
        plans.save(plan);
        return views.detail(plan, planSteps);
    }

    // ------------------------------------------------------------------ Helpers

    private List<String> environmentsOwnFirst() {
        Set<String> names = new LinkedHashSet<>();
        names.add(properties.orderable(properties.environment()) != null
            ? properties.orderable(properties.environment()) : properties.environment());
        names.addAll(properties.orderableEnvironments());
        return List.copyOf(names);
    }

    private String blockedReason(String environment, List<Deployer> environmentDeployers, boolean running, Instant now) {
        if (running) {
            return "A deployment of " + environment + " is running.";
        }
        if (environmentDeployers.isEmpty()) {
            return "No deployer is set up for " + environment + " yet.";
        }
        for (Deployer deployer : environmentDeployers) {
            if (deployer.getKind() == DeployerKind.core && !deployers.isOnline(deployer, now)) {
                return deployer.getLastSeenAt() == null
                    ? "The core deployer " + deployer.getName() + " has not connected yet."
                    : "The core deployer " + deployer.getName() + " was last seen "
                        + minutesSince(deployer.getLastSeenAt(), now) + " min ago.";
            }
        }
        return null;
    }

    private static long minutesSince(Instant then, Instant now) {
        return Math.max(1, Duration.between(then, now).toMinutes());
    }

    private List<String> suggestedTags() {
        Set<String> tags = new LinkedHashSet<>();
        tags.add("latest");
        plans.findTop30ByOrderByCreatedAtDesc().forEach(plan -> tags.add(plan.getTag()));
        return tags.stream().limit(MAX_SUGGESTED_TAGS).toList();
    }

    /** The name of the admin for the history. A user without a name shows the user name. */
    private String initiatorName(AuthContext auth) {
        if (auth == null || auth.userId() == null) {
            return "unknown";
        }
        return users.findById(auth.userId())
            .map(DeploymentService::displayName)
            .orElse("user " + auth.userId());
    }

    private static String displayName(User user) {
        String full = ((user.getPrename() == null ? "" : user.getPrename()) + " "
            + (user.getName() == null ? "" : user.getName())).strip();
        return full.isEmpty() ? user.getUsername() : full;
    }
}
