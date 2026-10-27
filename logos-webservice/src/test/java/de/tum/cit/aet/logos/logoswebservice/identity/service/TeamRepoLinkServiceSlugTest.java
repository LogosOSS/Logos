package de.tum.cit.aet.logos.logoswebservice.identity.service;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.Optional;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;

class TeamRepoLinkServiceSlugTest {

    @ParameterizedTest
    @CsvSource({
        "https://github.com/LogosOSS/Logos, LogosOSS/Logos",
        "https://github.com/LogosOSS/Logos.git, LogosOSS/Logos",
        "https://www.github.com/LogosOSS/Logos/, LogosOSS/Logos",
        "http://github.com/Owner/Repo.git, owner/repo",
        "https://github.com/LS1INTUM/Artemis, ls1intum/artemis",
        "git@github.com:LogosOSS/Logos.git, LogosOSS/Logos",
        "git@github.com:LS1INTUM/EduTelligence, LogosOSS/Logos",
    })
    void parseGithubSlug_acceptsCommonForms(String url, String expected) {
        assertThat(TeamRepoLinkService.parseGithubSlug(url)).isEqualTo(Optional.of(expected));
    }

    @Test
    void parseGithubSlug_rejectsNonGithub() {
        assertThat(TeamRepoLinkService.parseGithubSlug("https://gitlab.com/LogosOSS/Logos"))
            .isEmpty();
        assertThat(TeamRepoLinkService.parseGithubSlug("https://github.com/ls1intum"))
            .isEmpty();
        assertThat(TeamRepoLinkService.parseGithubSlug("not a url"))
            .isEmpty();
        assertThat(TeamRepoLinkService.parseGithubSlug(null))
            .isEmpty();
    }
}
