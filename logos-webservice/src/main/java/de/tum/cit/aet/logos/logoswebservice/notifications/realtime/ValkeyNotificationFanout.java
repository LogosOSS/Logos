package de.tum.cit.aet.logos.logoswebservice.notifications.realtime;

import io.valkey.springframework.data.valkey.connection.ValkeyConnectionFactory;
import io.valkey.springframework.data.valkey.core.StringValkeyTemplate;
import io.valkey.springframework.data.valkey.listener.ChannelTopic;
import io.valkey.springframework.data.valkey.listener.ValkeyMessageListenerContainer;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import com.fasterxml.jackson.databind.ObjectMapper;

/**
 * Multi-replica fan-out over Valkey pub/sub (the {@code logos-redis} instance
 * the gateway rate limiter already shares): the publishing replica sends the
 * event to one channel, and every replica, itself included, pushes it to its
 * own sockets. The default; {@code logos.notifications.fanout=local} opts out.
 *
 * <p>Pub/sub is fire-and-forget, which is enough here: the push is a hint
 * and the persisted inbox is the record. When Valkey is unreachable the
 * publishing replica still reaches its own sockets.
 */
@Configuration(NotificationFanout.BEAN_NAME)
@ConditionalOnProperty(name = "logos.notifications.fanout", havingValue = "redis", matchIfMissing = true)
public class ValkeyNotificationFanout implements NotificationFanout {

    static final String CHANNEL = "logos:notifications";

    private static final Logger log = LoggerFactory.getLogger(ValkeyNotificationFanout.class);

    private final StringValkeyTemplate valkey;
    private final NotificationSocketHandler sockets;
    private final ObjectMapper objectMapper;

    public ValkeyNotificationFanout(StringValkeyTemplate valkey, NotificationSocketHandler sockets,
                                    ObjectMapper objectMapper) {
        this.valkey = valkey;
        this.sockets = sockets;
        this.objectMapper = objectMapper;
    }

    @Override
    public void publish(NotificationEvent event) {
        try {
            valkey.convertAndSend(CHANNEL, objectMapper.writeValueAsString(event));
        } catch (Exception e) {
            log.warn("notification fan-out via Valkey failed; pushing to this replica only", e);
            sockets.deliverLocally(event);
        }
    }

    void receive(String body) {
        try {
            sockets.deliverLocally(objectMapper.readValue(body, NotificationEvent.class));
        } catch (Exception e) {
            log.warn("dropping unreadable notification fan-out message", e);
        }
    }

    @Bean
    ValkeyMessageListenerContainer notificationFanoutListener(ValkeyConnectionFactory connectionFactory) {
        ValkeyMessageListenerContainer container = new ValkeyMessageListenerContainer();
        container.setConnectionFactory(connectionFactory);
        container.addMessageListener(
            (message, pattern) -> receive(new String(message.getBody(), java.nio.charset.StandardCharsets.UTF_8)),
            new ChannelTopic(CHANNEL));
        return container;
    }
}
