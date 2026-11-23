package de.tum.cit.aet.logos.logoswebservice.identity.service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.http.HttpStatus;
import org.springframework.web.server.ResponseStatusException;

class TeamRepoLinkServiceEgressHostsTest {

    @Test
    void normalizeEgressHosts_trimsLowercasesDropsBlanksAndDedupesInOrder() {
        List<String> input = Arrays.asList(" GitHub.com ", "", null, "*.GitHubUserContent.com", "github.com", "  ");
        assertThat(TeamRepoLinkService.normalizeEgressHosts(input))
            .containsExactly("github.com", "*.githubusercontent.com");
    }

    @ParameterizedTest
    @ValueSource(strings = {
        "github.com",
        "*.githubusercontent.com",
        "lakecache.blob.core.windows.net",
        "*.co.uk",
        "a-b.example.org",
        "pypi.org",
    })
    void normalizeEgressHosts_acceptsHostnamesAndWildcards(String host) {
        assertThat(TeamRepoLinkService.normalizeEgressHosts(List.of(host))).containsExactly(host);
    }

    @ParameterizedTest
    @ValueSource(strings = {
        "https://github.com",
        "github.com:443",
        "github.com/path",
        "localhost",
        "10.0.0.1",
        "[::1]",
        "*.com",
        "*",
        "*github.com",
        "foo.*.com",
        "-bad.example.com",
        "bad-.example.com",
        "exa mple.com",
        "example.c",
        "user@github.com",
    })
    void normalizeEgressHosts_rejectsInvalidEntriesNamingThem(String host) {
        assertThatThrownBy(() -> TeamRepoLinkService.normalizeEgressHosts(List.of("github.com", host)))
            .isInstanceOfSatisfying(ResponseStatusException.class, e -> {
                assertThat(e.getStatusCode()).isEqualTo(HttpStatus.BAD_REQUEST);
                assertThat(e.getReason()).contains("'" + host + "'");
            });
    }

    @Test
    void normalizeEgressHosts_rejectsOverlongHostname() {
        String label = "a".repeat(60);
        String host = String.join(".", label, label, label, label, label) + ".com";
        assertThat(host.length()).isGreaterThan(253);
        assertThatThrownBy(() -> TeamRepoLinkService.normalizeEgressHosts(List.of(host)))
            .isInstanceOf(ResponseStatusException.class);
    }

    @Test
    void normalizeEgressHosts_limitsTheNumberOfEntries() {
        List<String> hosts = new ArrayList<>();
        for (int i = 0; i < 64; i++) {
            hosts.add("host" + i + ".example.com");
        }
        assertThat(TeamRepoLinkService.normalizeEgressHosts(hosts)).hasSize(64);
        hosts.add("host64.example.com");
        assertThatThrownBy(() -> TeamRepoLinkService.normalizeEgressHosts(hosts))
            .isInstanceOfSatisfying(ResponseStatusException.class,
                e -> assertThat(e.getReason()).contains("at most 64"));
    }

    @Test
    void normalizeEgressHosts_emptyListClearsTheHosts() {
        assertThat(TeamRepoLinkService.normalizeEgressHosts(List.of())).isEmpty();
        assertThat(TeamRepoLinkService.normalizeEgressHosts(List.of(" ", ""))).isEmpty();
    }
}
