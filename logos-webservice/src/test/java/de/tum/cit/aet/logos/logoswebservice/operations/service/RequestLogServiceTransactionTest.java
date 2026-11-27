package de.tum.cit.aet.logos.logoswebservice.operations.service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.reset;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

import java.util.List;
import java.util.Map;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.test.context.junit.jupiter.SpringJUnitConfig;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.annotation.EnableTransactionManagement;
import org.springframework.transaction.support.SimpleTransactionStatus;

import de.tum.cit.aet.logos.logoswebservice.operations.repository.LogEntryRepository;

@SpringJUnitConfig(RequestLogServiceTransactionTest.Config.class)
class RequestLogServiceTransactionTest {

    @Autowired
    RequestLogService service;

    @Autowired
    PlatformTransactionManager transactions;

    private final SimpleTransactionStatus transaction = new SimpleTransactionStatus();

    @BeforeEach
    void prepareTransactionManager() {
        reset(transactions);
        when(transactions.getTransaction(any())).thenReturn(transaction);
    }

    @Test
    void fullFilteredPageAndCountUseOneReadOnlySnapshot() {
        assertSnapshot(service.getLatestRequests(null, null, null, null, null, 17, false,
            null, null, null, 10, true, List.of(), List.of()));
    }

    @Test
    void teamActivityPageAndCountUseOneReadOnlySnapshot() {
        assertSnapshot(service.getLatestRequests(null, null, null, 7, null, null, null, 10, true));
    }

    @Test
    void legacyLivePageDoesNotOpenAServiceTransactionOrCount() {
        assertThat(service.getLatestRequests(null, null)).doesNotContainKey("total");
        verifyNoInteractions(transactions);
    }

    private void assertSnapshot(Map<String, Object> page) {
        assertThat(page).containsKey("total");
        var definition = ArgumentCaptor.forClass(TransactionDefinition.class);
        verify(transactions).getTransaction(definition.capture());
        assertThat(definition.getValue().isReadOnly()).isTrue();
        assertThat(definition.getValue().getIsolationLevel()).isEqualTo(TransactionDefinition.ISOLATION_REPEATABLE_READ);
        verify(transactions).commit(transaction);
    }

    @TestConfiguration(proxyBeanMethods = false)
    @EnableTransactionManagement
    static class Config {
        @Bean
        RequestLogService requestLogService() {
            return new RequestLogService(mock(LogEntryRepository.class), new ObjectMapper());
        }

        @Bean
        PlatformTransactionManager transactionManager() {
            return mock(PlatformTransactionManager.class);
        }
    }
}
