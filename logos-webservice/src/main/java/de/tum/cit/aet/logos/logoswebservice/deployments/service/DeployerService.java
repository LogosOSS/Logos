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
import de.tum.cit.aet.logos.logoswebservice.configuration.repository.ProviderIdentity;
import de.tum.cit.aet.logos.logoswebservice.configuration.repository.ProviderRepository;
import de.tum.cit.aet.logos.logoswebservice.deployments.DeploymentProperties;
import de.tum.cit.aet.logos.logoswebservice.deployments.entity.Deployer;
import de.tum.cit.aet.logos.logoswebservice.deployments.entity.DeployerKind;
import de.tum.cit.aet.logos.logoswebservice.deployments.repository.DeployerRepository;

/**
 * The deployers that obey this Logos: making them, checking their keys, and noting when they were seen.
 *
 * <p>A core deployer has a key of its own. It is found by the hash of the key, and the key itself is only ever in the
 * answer to the request that made it.
 *
 * <p>A worker deployer has no key of its own. It presents the worker key of its provider (LOGOS_API_KEY), the key that
 * the worker also uses with the orchestrator. Logos makes the row of the worker at its first contact, and the row
 * follows the name of the provider.
 */
@Service
public class DeployerService {

    private static final Pattern NAME = Pattern.compile("[A-Za-z0-9][A-Za-z0-9 ._-]{0,99}");
    private static final String BEARER = "Bearer ";
    private static final int MAX_VERSION_CHARS = 40;
    private static final int MAX_NAME_CHARS = 100;

    private final DeployerRepository deployers;
    private final ProviderRepository providers;
    private final DeploymentProperties properties;
    private Clock clock = Clock.systemUTC();

    public DeployerService(DeployerRepository deployers, ProviderRepository providers, DeploymentProperties properties) {
        this.deployers = deployers;
        this.providers = providers;
        this.properties = properties;
    }

    void setClock(Clock clock) {
        this.clock = clock;
    }

    /** A deployer that was just made, with the key that is shown once. */
    public record Created(Deployer deployer, String key) {}

    /** All deployers, the core one first. */
    @Transactional(readOnly = true)
    public List<Deployer> list() {
        return deployers.findAllByOrderByKindAscNameAsc();
    }

    /** Make a core deployer, with a key of its own. A worker needs no deployer made: see {@link #authenticate}. */
    @Transactional
    public Created create(String name, String kind, Integer createdBy) {
        String cleanName = name == null ? "" : name.strip();
        if (!NAME.matcher(cleanName).matches()) {
            throw new IllegalArgumentException(
                "The name needs 1 to 100 letters, digits, spaces, '.', '_' or '-', and must start with a letter or digit.");
        }
        requireCore(kind);
        if (deployers.existsByNameIgnoreCase(cleanName)) {
            throw new ConflictException("A deployer called '" + cleanName + "' exists already.");
        }
        String key = DeployerKeys.newKey();
        Deployer deployer = new Deployer();
        deployer.setId(UUID.randomUUID());
        deployer.setName(cleanName);
        deployer.setKind(DeployerKind.core);
        deployer.setKeyHash(DeployerKeys.hash(key));
        deployer.setCreatedAt(clock.instant());
        deployer.setCreatedBy(createdBy);
        return new Created(deployers.save(deployer), key);
    }

    /** Give a core deployer a new key. The old key stops working at once. A worker has no key of its own. */
    @Transactional
    public Created replaceKey(UUID id) {
        Deployer deployer = deployers.findById(id).orElseThrow(() -> new NotFoundException("There is no such deployer."));
        if (deployer.getKind() != DeployerKind.core) {
            throw new IllegalArgumentException(
                "A worker has no deployer key. Its key is the worker key of its provider: change it on the page Providers.");
        }
        String key = DeployerKeys.newKey();
        deployer.setKeyHash(DeployerKeys.hash(key));
        return new Created(deployers.save(deployer), key);
    }

    @Transactional
    public void delete(UUID id) {
        Deployer deployer = deployers.findById(id).orElseThrow(() -> new NotFoundException("There is no such deployer."));
        deployers.delete(deployer);
    }

    /**
     * The deployer that the {@code Authorization: Bearer} header names, if the key is one of ours: the key of a core
     * deployer, or the worker key of a logosnode provider. The first contact of a worker makes its row.
     *
     * <p>Which kind a deployer is follows from its key, never from what the request says about itself.
     */
    @Transactional
    public Optional<Deployer> authenticate(String authorization) {
        if (authorization == null || !authorization.regionMatches(true, 0, BEARER, 0, BEARER.length())) {
            return Optional.empty();
        }
        String key = authorization.substring(BEARER.length()).strip();
        if (key.isEmpty() || key.length() > 200) {
            return Optional.empty();
        }
        Optional<Deployer> core = deployers.findByKeyHash(DeployerKeys.hash(key));
        return core.isPresent() ? core : workerWithKey(key);
    }

    /**
     * The worker whose provider has this key. A key that no logosnode provider has is not a worker key, and neither is
     * one that several providers share: the caller cannot be told from the others.
     */
    private Optional<Deployer> workerWithKey(String key) {
        List<ProviderIdentity> owners = providers.findLogosnodeByApiKey(key);
        if (owners.size() != 1) {
            return Optional.empty();
        }
        ProviderIdentity provider = owners.get(0);
        String name = workerName(provider);
        Optional<Deployer> known = deployers.findByProviderId(provider.getId());
        if (known.isPresent() && name.equals(known.get().getName())) {
            return known;
        }
        if (known.isPresent()) {
            deployers.rename(known.get().getId(), name);
        } else {
            deployers.insertWorkerIfAbsent(UUID.randomUUID(), name, provider.getId());
        }
        return deployers.findByProviderId(provider.getId());
    }

    /** The name of the provider, cut to the length of the column. A provider without a name gets its number. */
    private static String workerName(ProviderIdentity provider) {
        String name = provider.getName() == null ? "" : provider.getName().strip();
        if (name.isEmpty()) {
            return "worker-" + provider.getId();
        }
        return name.length() > MAX_NAME_CHARS ? name.substring(0, MAX_NAME_CHARS) : name;
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

    /** Only a core deployer is made. A request without a kind means core. */
    private static void requireCore(String kind) {
        String clean = kind == null ? "" : kind.strip();
        if (clean.isEmpty() || clean.equals(DeployerKind.core.name())) {
            return;
        }
        if (clean.equals(DeployerKind.worker.name())) {
            throw new IllegalArgumentException(
                "A worker needs no deployer made. It uses the worker key of its provider (LOGOS_API_KEY) and shows up "
                    + "here after its node manager asked Logos for orders.");
        }
        throw new IllegalArgumentException("The kind must be 'core'.");
    }
}
