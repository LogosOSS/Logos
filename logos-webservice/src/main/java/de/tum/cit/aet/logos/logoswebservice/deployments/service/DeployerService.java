package de.tum.cit.aet.logos.logoswebservice.deployments.service;

import java.time.Clock;
import java.time.Instant;
import java.util.List;
import java.util.Optional;
import java.util.UUID;
import java.util.regex.Pattern;

import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import de.tum.cit.aet.logos.logoswebservice.common.ConflictException;
import de.tum.cit.aet.logos.logoswebservice.common.NotFoundException;
import de.tum.cit.aet.logos.logoswebservice.deployments.DeploymentProperties;
import de.tum.cit.aet.logos.logoswebservice.deployments.entity.Deployer;
import de.tum.cit.aet.logos.logoswebservice.deployments.entity.DeployerKind;
import de.tum.cit.aet.logos.logoswebservice.deployments.repository.DeployerRepository;

/**
 * The deployers that obey this Logos: making them, checking their keys, and noting when they were seen. A deployer is
 * found by the hash of its key. The key itself is only ever in the answer to the request that made it.
 */
@Service
public class DeployerService {

    private static final Pattern NAME = Pattern.compile("[A-Za-z0-9][A-Za-z0-9 ._-]{0,99}");
    private static final String BEARER = "Bearer ";
    private static final int MAX_VERSION_CHARS = 40;

    private final DeployerRepository deployers;
    private final DeploymentProperties properties;
    private Clock clock = Clock.systemUTC();

    public DeployerService(DeployerRepository deployers, DeploymentProperties properties) {
        this.deployers = deployers;
        this.properties = properties;
    }

    void setClock(Clock clock) {
        this.clock = clock;
    }

    /** A deployer that was just made, with the key that is shown once. */
    public record Created(Deployer deployer, String key) {}

    @Transactional(readOnly = true)
    public List<Deployer> list() {
        return deployers.findAllByOrderByEnvironmentAscKindAscNameAsc();
    }

    @Transactional(readOnly = true)
    public List<Deployer> listFor(String environment) {
        return deployers.findByEnvironmentOrderByKindAscNameAsc(environment);
    }

    @Transactional
    public Created create(String name, String environment, String kind, Integer createdBy) {
        String cleanName = name == null ? "" : name.strip();
        if (!NAME.matcher(cleanName).matches()) {
            throw new IllegalArgumentException(
                "The name needs 1 to 100 letters, digits, spaces, '.', '_' or '-', and must start with a letter or digit.");
        }
        String env = properties.orderable(environment);
        if (env == null) {
            throw new IllegalArgumentException("This Logos does not deploy the environment '" + environment + "'.");
        }
        DeployerKind deployerKind = parseKind(kind);
        if (deployers.existsByEnvironmentIgnoreCaseAndNameIgnoreCase(env, cleanName)) {
            throw new ConflictException("The environment " + env + " has a deployer called '" + cleanName + "' already.");
        }
        String key = DeployerKeys.newKey();
        Deployer deployer = new Deployer();
        deployer.setId(UUID.randomUUID());
        deployer.setName(cleanName);
        deployer.setEnvironment(env);
        deployer.setKind(deployerKind);
        deployer.setKeyHash(DeployerKeys.hash(key));
        deployer.setCreatedAt(clock.instant());
        deployer.setCreatedBy(createdBy);
        return new Created(deployers.save(deployer), key);
    }

    /** Give a deployer a new key. The old key stops working at once. */
    @Transactional
    public Created replaceKey(UUID id) {
        Deployer deployer = deployers.findById(id).orElseThrow(() -> new NotFoundException("There is no such deployer."));
        String key = DeployerKeys.newKey();
        deployer.setKeyHash(DeployerKeys.hash(key));
        return new Created(deployers.save(deployer), key);
    }

    @Transactional
    public void delete(UUID id) {
        Deployer deployer = deployers.findById(id).orElseThrow(() -> new NotFoundException("There is no such deployer."));
        deployers.delete(deployer);
    }

    /** The deployer that the {@code Authorization: Bearer} header names, if the key is one of ours. */
    @Transactional(readOnly = true)
    public Optional<Deployer> authenticate(String authorization) {
        if (authorization == null || !authorization.regionMatches(true, 0, BEARER, 0, BEARER.length())) {
            return Optional.empty();
        }
        String key = authorization.substring(BEARER.length()).strip();
        if (key.isEmpty() || key.length() > 200) {
            return Optional.empty();
        }
        return deployers.findByKeyHash(DeployerKeys.hash(key));
    }

    /** Note that the deployer is alive. Only the columns of the heartbeat are written: see the repository. */
    @Transactional
    public void touch(UUID id, String version) {
        if (version == null || version.isBlank()) {
            deployers.markSeen(id, clock.instant());
            return;
        }
        String clean = version.strip();
        deployers.markSeenWithVersion(id, clock.instant(), clean.substring(0, Math.min(clean.length(), MAX_VERSION_CHARS)));
    }

    public boolean isOnline(Deployer deployer, Instant now) {
        return deployer.getLastSeenAt() != null
            && deployer.getLastSeenAt().isAfter(now.minus(properties.onlineAfter()));
    }

    private static DeployerKind parseKind(String kind) {
        try {
            return DeployerKind.valueOf(kind == null ? "" : kind.strip());
        } catch (IllegalArgumentException e) {
            throw new IllegalArgumentException("The kind must be 'core' or 'worker'.");
        }
    }
}
