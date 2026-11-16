package de.tum.cit.aet.logos.logoswebservice.gateway;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.task.SimpleAsyncTaskExecutor;

@Configuration(proxyBeanMethods = false)
public class GatewayResponseExecutorConfiguration {
    /** Separate from Boot's background executor; an optional cap bounds I/O without queuing response tasks. */
    @Bean(name = "gatewayResponseExecutor", defaultCandidate = false, destroyMethod = "close")
    public SimpleAsyncTaskExecutor gatewayResponseExecutor() {
        SimpleAsyncTaskExecutor executor = new SimpleAsyncTaskExecutor("logos-response-");
        executor.setVirtualThreads(true);
        executor.setCancelRemainingTasksOnClose(true);
        executor.setTaskTerminationTimeout(1000);
        return executor;
    }
}
