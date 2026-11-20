package de.tum.cit.aet.logos.logoswebservice.common;

import java.lang.annotation.ElementType;
import java.lang.annotation.Retention;
import java.lang.annotation.RetentionPolicy;
import java.lang.annotation.Target;

/** Opts a read-only JSON POST handler into safe, idempotent HTTP QUERY requests. */
@Target(ElementType.METHOD)
@Retention(RetentionPolicy.RUNTIME)
public @interface ReadOnlyQuery {
}
