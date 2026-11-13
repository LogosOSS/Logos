package de.tum.cit.aet.logos.logoswebservice.deployments;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import java.util.Map;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.context.annotation.Import;
import org.springframework.security.oauth2.jwt.JwtDecoder;
import org.springframework.test.context.TestPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.context.jdbc.Sql;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.ResultActions;

import com.fasterxml.jackson.databind.ObjectMapper;

import de.tum.cit.aet.logos.logoswebservice.TestContainersConfig;
import de.tum.cit.aet.logos.logoswebservice.TestJwt;

/**
 * A Logos that nobody configured is protected (Prod): its page asks for a confirmation, and it accepts only
 * {@code latest} and release tags. The test does not set {@code logos.deployments.protect}, so it also pins that
 * default. Dev and Test switch the protection off.
 */
@SpringBootTest
@AutoConfigureMockMvc
@Import(TestContainersConfig.class)
@TestPropertySource(properties = {
    "spring.liquibase.enabled=true",
    "spring.liquibase.change-log=classpath:liquibase/changelog/master.xml",
    "logos.auth.roles.logos-admin=itg-admin",
    "logos.auth.roles.app-admin=chair-member",
    "logos.auth.sync-debounce-minutes=5",
})
@Sql(scripts = "/sql/seed-identity.sql", executionPhase = Sql.ExecutionPhase.BEFORE_TEST_METHOD)
@Sql(statements = {
    "DELETE FROM deployment_steps",
    "DELETE FROM deployment_plans",
    "DELETE FROM deployers",
}, executionPhase = Sql.ExecutionPhase.AFTER_TEST_METHOD)
@Sql(scripts = "/sql/cleanup-identity.sql", executionPhase = Sql.ExecutionPhase.AFTER_TEST_METHOD)
class DeploymentProtectionTest {

    @Autowired MockMvc mvc;
    @Autowired ObjectMapper json;
    @MockitoBean JwtDecoder jwtDecoder;

    private ResultActions rawStart(String tag) throws Exception {
        return mvc.perform(post("/deployments/plans")
            .with(TestJwt.logosAdmin())
            .contentType("application/json")
            .content(json.writeValueAsString(Map.of("tag", tag))));
    }

    /** A core deployer that was seen just now, so that nothing but the protection can refuse a start. */
    private void onlineCoreDeployer() throws Exception {
        String key = json.readTree(mvc.perform(post("/deployments/deployers")
                .with(TestJwt.logosAdmin())
                .contentType("application/json")
                .content("{\"name\":\"prod-core\",\"kind\":\"core\"}"))
            .andExpect(status().isCreated())
            .andReturn().getResponse().getContentAsString()).get("key").asText();
        mvc.perform(post("/deployments/deployer/poll")
                .header("Authorization", "Bearer " + key)
                .contentType("application/json")
                .content("{\"wait_s\":0,\"kind\":\"core\",\"version\":\"0.1.0\"}"))
            .andExpect(status().isOk());
    }

    @Test
    void theOverviewTellsThePageThatItHasToAskForAConfirmation() throws Exception {
        mvc.perform(get("/deployments/overview").with(TestJwt.logosAdmin()))
            .andExpect(status().isOk())
            .andExpect(jsonPath("$.requires_confirmation").value(true));
    }

    @Test
    void aProtectedLogosAcceptsOnlyLatestAndReleaseTags() throws Exception {
        onlineCoreDeployer();

        rawStart("pr-93")
            .andExpect(status().isBadRequest())
            .andExpect(jsonPath("$.error").value("This Logos accepts only 'latest' and release tags, not 'pr-93'."));
        rawStart("main").andExpect(status().isBadRequest());
        rawStart("latest").andExpect(status().isCreated());
    }
}
