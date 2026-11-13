package de.tum.cit.aet.logos.logoswebservice.configuration;

/** Validation shared by model creation, renaming, aliases and imports. */
public final class ModelNames {
    private ModelNames() {}

    public static void rejectControlCharacters(String name) {
        if (name != null && name.codePoints().anyMatch(Character::isISOControl)) {
            throw new IllegalArgumentException("Model names must not contain control characters");
        }
    }
}
