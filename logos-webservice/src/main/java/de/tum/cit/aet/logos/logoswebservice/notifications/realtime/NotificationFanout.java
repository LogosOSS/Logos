package de.tum.cit.aet.logos.logoswebservice.notifications.realtime;

/**
 * Gets a published notification to the sockets of every application server
 * replica. A notification is published on one replica, but its recipients'
 * sockets can be open on any of them.
 */
public interface NotificationFanout {

    /**
     * The one bean name both implementations register under. Their
     * {@code logos.notifications.fanout} conditions are mutually exclusive,
     * so exactly one bean carries it in any configuration.
     */
    String BEAN_NAME = "notificationFanout";

    void publish(NotificationEvent event);
}
