package de.tum.cit.aet.logos.logoswebservice.configuration;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertThrows;

import org.junit.jupiter.api.Test;

class ModelNamesTest {
    @Test
    void rejectsEveryC0AndC1ControlCharacter() {
        for (int code = 0; code <= 159; code++) {
            if (code >= 32 && code < 127) continue;
            String name = "org/model" + (char) code;
            assertThrows(IllegalArgumentException.class, () -> ModelNames.rejectControlCharacters(name));
        }
    }

    @Test
    void permitsPrintableUnicodeAndShellMetacharacters() {
        assertDoesNotThrow(() -> ModelNames.rejectControlCharacters("org/模型-💡 '$()`;[1m]"));
        assertDoesNotThrow(() -> ModelNames.rejectControlCharacters(null));
    }
}
