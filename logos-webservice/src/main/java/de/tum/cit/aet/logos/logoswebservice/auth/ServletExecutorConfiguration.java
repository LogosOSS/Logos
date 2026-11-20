package de.tum.cit.aet.logos.logoswebservice.auth;

import org.apache.coyote.ProtocolHandler;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.boot.tomcat.TomcatProtocolHandlerCustomizer;
import org.springframework.context.SmartLifecycle;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.task.SimpleAsyncTaskExecutor;

/** Virtual HTTP handling includes authentication, routing and waiting for upstream headers. */
@Configuration(proxyBeanMethods = false)
public class ServletExecutorConfiguration {

    @Bean(name = "servletRequestExecutor", defaultCandidate = false, destroyMethod = "close")
    public SimpleAsyncTaskExecutor servletRequestExecutor() {
        SimpleAsyncTaskExecutor executor = new SimpleAsyncTaskExecutor("logos-request-");
        executor.setVirtualThreads(true);
        executor.setCancelRemainingTasksOnClose(true);
        executor.setTaskTerminationTimeout(5000);
        return executor;
    }

    @Bean
    public TomcatProtocolHandlerCustomizer<ProtocolHandler> servletVirtualThreadCustomizer(
            @Qualifier("servletRequestExecutor") SimpleAsyncTaskExecutor executor) {
        return protocol -> protocol.setExecutor(executor);
    }

    /** Stops request work after the connector, while accounting and its dependencies are still alive. */
    @Bean
    public SmartLifecycle servletExecutorShutdown(
            @Qualifier("servletRequestExecutor") SimpleAsyncTaskExecutor executor) {
        return new SmartLifecycle() {
            private volatile boolean running;

            @Override public void start() {
                if (!executor.isActive()) throw new IllegalStateException("Servlet request executor is closed");
                running = true;
            }

            @Override public void stop() {
                try {
                    executor.close();
                } finally {
                    running = false;
                }
            }

            @Override public boolean isRunning() { return running; }

            // A cached context may pause and restart. Only terminal shutdown closes this executor.
            @Override public boolean isPauseable() { return false; }

            @Override public int getPhase() {
                // After Boot's connector-stop phase, before singleton dependencies are destroyed.
                return SmartLifecycle.DEFAULT_PHASE - 3072;
            }
        };
    }
}
