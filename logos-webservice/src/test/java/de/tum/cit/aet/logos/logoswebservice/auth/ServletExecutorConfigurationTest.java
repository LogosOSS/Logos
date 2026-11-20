package de.tum.cit.aet.logos.logoswebservice.auth;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.concurrent.TimeUnit;

import org.junit.jupiter.api.Test;
import org.springframework.context.annotation.AnnotationConfigApplicationContext;
import org.springframework.core.task.SimpleAsyncTaskExecutor;

class ServletExecutorConfigurationTest {
    @Test
    void pauseAndRestartKeepTheExecutorAliveButCloseStopsIt() throws Exception {
        SimpleAsyncTaskExecutor executor;
        try (var context = new AnnotationConfigApplicationContext(ServletExecutorConfiguration.class)) {
            executor = context.getBean("servletRequestExecutor", SimpleAsyncTaskExecutor.class);
            context.pause();
            assertThat(executor.isActive()).isTrue();
            context.restart();
            Thread thread = executor.submitCompletable(Thread::currentThread).get(2, TimeUnit.SECONDS);
            assertThat(thread.isVirtual()).isTrue();
            assertThat(thread.getName()).startsWith("logos-request-");
        }
        assertThat(executor.isActive()).isFalse();
    }
}
