package de.tum.cit.aet.logos.logoswebservice.audit;

import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Propagation;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.context.request.RequestAttributes;
import org.springframework.web.context.request.RequestContextHolder;

import de.tum.cit.aet.logos.logoswebservice.auth.AuthContext;
import jakarta.persistence.EntityManager;

/** Supplies trusted, transaction-local metadata to the permission audit triggers. */
@Component
public class PermissionAuditContext {
    private final EntityManager entityManager;

    public PermissionAuditContext(EntityManager entityManager) {
        this.entityManager = entityManager;
    }

    @Transactional(propagation = Propagation.MANDATORY)
    public void withCause(String cause, Runnable mutation) {
        // Hibernate must execute queued writes before changing their attribution.
        entityManager.flush();
        Object[] previous = (Object[]) entityManager.createNativeQuery("""
            SELECT current_setting('logos.permission_actor_id', true),
                   current_setting('logos.permission_actor_role', true),
                   current_setting('logos.permission_cause', true)
            """, Object[].class).getSingleResult();
        AuthContext actor = currentActor();
        boolean system = actor == null || "cascade".equals(cause);
        setContext(system || actor.userId() == null ? "" : actor.userId().toString(),
                   system ? "system" : actor.role(), cause);
        try {
            mutation.run();
            entityManager.flush();
        } catch (RuntimeException | Error failure) {
            try {
                setContext(previous[0], previous[1], previous[2]);
            } catch (RuntimeException restoreFailure) {
                // A failed SQL statement may already have aborted the transaction.
                failure.addSuppressed(restoreFailure);
            }
            throw failure;
        }
        setContext(previous[0], previous[1], previous[2]);
    }

    private void setContext(Object actorId, Object actorRole, Object cause) {
        entityManager.createNativeQuery("""
            SELECT set_config('logos.permission_actor_id', :actorId, true),
                   set_config('logos.permission_actor_role', :actorRole, true),
                   set_config('logos.permission_cause', :cause, true)
            """, Object[].class)
            .setParameter("actorId", actorId == null ? "" : actorId.toString())
            .setParameter("actorRole", actorRole == null ? "" : actorRole.toString())
            .setParameter("cause", cause == null ? "" : cause.toString())
            .getSingleResult();
    }

    private static AuthContext currentActor() {
        RequestAttributes attrs = RequestContextHolder.getRequestAttributes();
        if (attrs == null) return null;
        Object ctx = attrs.getAttribute("authContext", RequestAttributes.SCOPE_REQUEST);
        return ctx instanceof AuthContext actor ? actor : null;
    }
}
