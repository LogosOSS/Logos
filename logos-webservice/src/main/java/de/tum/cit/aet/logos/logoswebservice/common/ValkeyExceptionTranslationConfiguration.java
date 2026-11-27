package de.tum.cit.aet.logos.logoswebservice.common;

import glide.api.models.exceptions.GlideException;
import io.valkey.springframework.data.valkey.connection.valkeyglide.ValkeyGlideConnectionFactory;
import org.aopalliance.intercept.MethodInterceptor;
import org.springframework.aop.framework.ProxyFactory;
import org.springframework.aop.support.DefaultPointcutAdvisor;
import org.springframework.aop.support.NameMatchMethodPointcut;
import org.springframework.beans.factory.config.BeanPostProcessor;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/** Keep the Valkey translator from claiming unrelated repository failures. */
@Configuration(proxyBeanMethods = false)
class ValkeyExceptionTranslationConfiguration {

    @Bean
    static BeanPostProcessor scopeValkeyExceptionTranslation() {
        return new BeanPostProcessor() {
            @Override
            public Object postProcessAfterInitialization(Object bean, String beanName) {
                if (!(bean instanceof ValkeyGlideConnectionFactory)) {
                    return bean;
                }

                // Spring offers repository exceptions to all connection-factory
                // translators. GLIDE's translator otherwise wraps unknown errors as
                // ValkeySystemException, including errors from Postgres repositories.
                var translate = new NameMatchMethodPointcut();
                translate.setMappedName("translateExceptionIfPossible");
                MethodInterceptor glideOnly = invocation ->
                    invocation.getArguments()[0] instanceof GlideException ? invocation.proceed() : null;

                // Leave the starter's connection settings and factory lifecycle intact.
                var proxy = new ProxyFactory(bean);
                proxy.setProxyTargetClass(true);
                proxy.addAdvisor(new DefaultPointcutAdvisor(translate, glideOnly));
                return proxy.getProxy();
            }
        };
    }
}
