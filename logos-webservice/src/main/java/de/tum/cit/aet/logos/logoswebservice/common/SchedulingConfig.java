package de.tum.cit.aet.logos.logoswebservice.common;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.scheduling.concurrent.ThreadPoolTaskScheduler;

@Configuration
public class SchedulingConfig {

    /**
     * Dedicated single-thread scheduler for the Hugging Face recheck.
     *
     * A recheck pass makes blocking remote round trips — a precheck and a
     * license fetch per pending, flagged or unlicensed row — and can run for
     * minutes. On the shared default scheduler that would occupy the one
     * scheduling thread and delay the other {@code @Scheduled} jobs, above
     * all the gateway accounting sweeps; this keeps it on its own thread.
     * One thread also guarantees that at most one pass is in flight.
     */
    @Bean(destroyMethod = "shutdown")
    public ThreadPoolTaskScheduler hfRecheckScheduler() {
        ThreadPoolTaskScheduler scheduler = new ThreadPoolTaskScheduler();
        scheduler.setPoolSize(1);
        scheduler.setThreadNamePrefix("hf-recheck-");
        return scheduler;
    }
}
