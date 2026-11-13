package de.tum.cit.aet.logos.logoswebservice.operations.controller;

import java.nio.charset.StandardCharsets;
import java.util.List;

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

import de.tum.cit.aet.logos.logoswebservice.TestContainersConfig;
import de.tum.cit.aet.logos.logoswebservice.TestJwt;
import de.tum.cit.aet.logos.logoswebservice.operations.service.BatchService;
import de.tum.cit.aet.logos.logoswebservice.operations.service.BatchPriceService;

import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;

/**
 * The batch page is an admin facility: app developers are refused before the
 * proxy may run, app admins and logos admins get through.
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
    "logos.orchestrator.url=",
    "logos.orchestrator.internal-secret="
})
@Sql(scripts = {"/sql/seed-identity.sql"}, executionPhase = Sql.ExecutionPhase.BEFORE_TEST_METHOD)
@Sql(scripts = {"/sql/cleanup-identity.sql"}, executionPhase = Sql.ExecutionPhase.AFTER_TEST_METHOD)
class BatchControllerAccessTest {

    @Autowired MockMvc mvc;
    @MockitoBean JwtDecoder jwtDecoder;
    @MockitoBean BatchService batchService;
    @MockitoBean BatchPriceService batchPriceService;

    @Test
    void appDeveloperCannotReadPrices() throws Exception {
        mvc.perform(get("/logosdb/batches/prices").param("apiKeyId", "3004").with(TestJwt.testUser()))
            .andExpect(status().isForbidden());
    }

    @Test
    void bothAdminRolesCanReadSelectedKeyPrices() throws Exception {
        when(batchPriceService.pricesForKey(anyInt(), anyInt())).thenReturn(List.of(
            new BatchPriceService.ModelPrice("model", 100L, 200L)));
        for (var user : List.of(TestJwt.adminUser(), TestJwt.logosAdmin())) {
            mvc.perform(get("/logosdb/batches/prices").param("apiKeyId", "3004").with(user))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$[0].model_name").value("model"))
                .andExpect(jsonPath("$[0].input_price_per_k_tokens").value(100))
                .andExpect(jsonPath("$[0].output_price_per_k_tokens").value(200));
        }
    }

    @Test
    void pricesForAnUnownedKeyAreForbidden() throws Exception {
        when(batchPriceService.pricesForKey(anyInt(), anyInt()))
            .thenThrow(new BatchService.KeyNotOwnedException("This API key does not belong to you."));
        mvc.perform(get("/logosdb/batches/prices").param("apiKeyId", "3004").with(TestJwt.adminUser()))
            .andExpect(status().isForbidden());
    }


    @Test
    void appDeveloperIsRefused() throws Exception {
        mvc.perform(get("/logosdb/batches")
                .param("apiKeyId", "3004")
                .with(TestJwt.testUser()))
           .andExpect(status().isForbidden());
    }

    @Test
    void appAdminMayList() throws Exception {
        when(batchService.listBatches(anyInt(), anyInt()))
            .thenReturn(new BatchService.ProxiedResponse(
                200, "application/json", "{\"data\":[]}".getBytes(StandardCharsets.UTF_8)));

        mvc.perform(get("/logosdb/batches")
                .param("apiKeyId", "3004")
                .with(TestJwt.adminUser()))
           .andExpect(status().isOk());
    }

    @Test
    void logosAdminMayList() throws Exception {
        when(batchService.listBatches(anyInt(), anyInt()))
            .thenReturn(new BatchService.ProxiedResponse(
                200, "application/json", "{\"data\":[]}".getBytes(StandardCharsets.UTF_8)));

        mvc.perform(get("/logosdb/batches")
                .param("apiKeyId", "3004")
                .with(TestJwt.logosAdmin()))
           .andExpect(status().isOk());
    }

    @Test
    void anInvalidBatchIdIsABadRequest() throws Exception {
        when(batchService.getBatch(anyInt(), anyInt(), anyString()))
            .thenThrow(new BatchService.InvalidIdException("Not a valid batch or file id."));

        mvc.perform(get("/logosdb/batches/{batchId}", "bad id")
                .param("apiKeyId", "3004")
                .with(TestJwt.logosAdmin()))
           .andExpect(status().isBadRequest());
    }
}
