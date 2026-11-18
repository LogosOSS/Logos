package de.tum.cit.aet.logos.logoswebservice.billing;

import static org.assertj.core.api.Assertions.assertThat;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.regex.Pattern;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.oauth2.jwt.JwtDecoder;
import org.springframework.test.annotation.DirtiesContext;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.junit.jupiter.Container;
import org.testcontainers.junit.jupiter.Testcontainers;

/**
 * Testcontainers checks for the changelog-022 pricing engine: schema shape,
 * {@code logos_resolve_unit_price}, {@code logos_price_usage}, the
 * {@code log_entry_cost} / {@code budget_usage} views, and preservation of price history.
 */
@SpringBootTest
@Testcontainers
@DirtiesContext(classMode = DirtiesContext.ClassMode.AFTER_CLASS)
class TokenCostFunctionTest {

    @Autowired
    JdbcTemplate jdbc;

    @MockitoBean
    JwtDecoder jwtDecoder;

    @Container
    @SuppressWarnings("resource")
    static PostgreSQLContainer<?> postgres = new PostgreSQLContainer<>("postgres:17")
            .withDatabaseName("logosdb")
            .withUsername("postgres")
            .withPassword("root");

    @DynamicPropertySource
    @SuppressWarnings("unused")
    static void configureProperties(DynamicPropertyRegistry registry) {
        registry.add("spring.datasource.url", postgres::getJdbcUrl);
        registry.add("spring.datasource.username", postgres::getUsername);
        registry.add("spring.datasource.password", postgres::getPassword);
        registry.add("spring.datasource.driver-class-name", () -> "org.postgresql.Driver");
        registry.add("spring.liquibase.enabled", () -> "true");
        registry.add("spring.liquibase.change-log", () -> "classpath:liquibase/changelog/master.xml");
        registry.add("spring.jpa.hibernate.ddl-auto", () -> "validate");
    }

    private static final AtomicInteger SEQ = new AtomicInteger(1);

    // ---------------------------------------------------------------- schema

    @Test
    void migration022_addsColumnsAndCanonicalTypes() {
        assertThat(columnExists("token_prices", "unit")).isTrue();
        assertThat(columnExists("token_prices", "min_context_tokens")).isTrue();
        assertThat(columnExists("token_prices", "service_tier")).isTrue();
        assertThat(columnExists("token_prices", "price_per_k_unit")).isTrue();
        assertThat(columnExists("token_prices", "price_per_k_token")).isFalse();
        assertThat(columnExists("log_entry", "service_tier")).isTrue();
        assertThat(indexExists("log_entry", "idx_log_entry_api_key_timestamp_request")).isTrue();

        Integer newTypes = jdbc.queryForObject(
            "SELECT COUNT(*) FROM token_types WHERE name IN "
            + "('billed_input_uncached','billed_output_text','billed_input_cache_read',"
            + "'billed_input_cache_write','billed_input_cache_write_1h','billed_requests',"
            + "'billed_input_characters','billed_input_images','billed_output_images',"
            + "'billed_input_pixels','billed_output_pixels',"
            + "'billed_ocr_pages','billed_search_queries')", Integer.class);
        assertThat(newTypes).isEqualTo(13);
    }

    // ---------------------------------------------------- resolve_unit_price

    @Test
    void resolveUnitPrice_fallsBackAndPicksTierAndServiceTier() {
        int model = seedModel();
        int provider = seedCloudProvider("openai");
        seedPrice(model, provider, "billed_input_uncached", "token", 0, "default", 20000, "2020-01-01");
        seedPrice(model, provider, "billed_input_uncached", "token", 200000, "default", 40000, "2020-01-01");
        seedPrice(model, provider, "billed_input_uncached", "token", 0, "flex", 10000, "2020-01-01");

        assertThat(resolve("billed_input_cache_read", model, provider, "2026-01-01", 1000L, null))
            .isEqualTo(20000L);
        assertThat(resolve("billed_input_uncached", model, provider, "2026-01-01", 500000L, null))
            .isEqualTo(40000L);
        assertThat(resolve("billed_input_uncached", model, provider, "2026-01-01", 1000L, "flex"))
            .isEqualTo(10000L);
        assertThat(resolve("billed_output_text", model, provider, "2026-01-01", 1000L, null))
            .isNull();
    }

    @Test
    void resolveUnitPrice_cachedAudioFallsBackTransitivelyToBaseInputRate() {
        int model = seedModel();
        int provider = seedCloudProvider("openai");
        seedPrice(model, provider, "billed_input_uncached", "token", 0, "default", 20000, "2020-01-01");

        assertThat(resolve("billed_input_audio_cache_read", model, provider,
            "2026-01-01", 1000L, null)).isEqualTo(20000L);
        assertThat(resolve("billed_input_audio_cache_write", model, provider,
            "2026-01-01", 1000L, null)).isEqualTo(20000L);
    }

    // -------------------------------------------------------- price_usage

    @Test
    void priceUsage_openAiInclusive_appliesCacheDiscountOnce() {
        int model = seedModel();
        int provider = seedCloudProvider("openai");
        seedPrice(model, provider, "billed_input_uncached", "token", 0, "default", 20000, "2020-01-01");
        seedPrice(model, provider, "billed_input_cache_read", "token", 0, "default", 2000, "2020-01-01");
        seedPrice(model, provider, "billed_output_text", "token", 0, "default", 120000, "2020-01-01");

        Long cost = price(model, provider, "2026-01-01", null,
            "{\"prompt_tokens\":1000,\"prompt_cached_tokens\":800,\"completion_tokens\":300}");
        // 200*20000/1000 + 800*2000/1000 + 300*120000/1000
        assertThat(cost).isEqualTo(4000L + 1600L + 36000L);
    }

    @Test
    void priceUsage_openAiInclusive_appliesCacheWritePremiumOnce() {
        int model = seedModel();
        int provider = seedCloudProvider("openai");
        seedPrice(model, provider, "billed_input_uncached", "token", 0, "default", 20000, "2020-01-01");
        seedPrice(model, provider, "billed_input_cache_write", "token", 0, "default", 25000, "2020-01-01");

        Long cost = price(model, provider, "2026-01-01", null,
            "{\"prompt_tokens\":1000,\"prompt_cache_write_tokens\":200}");
        // Cache writes are a subset of OpenAI/Azure prompt_tokens.
        assertThat(cost).isEqualTo(800L * 20000 / 1000 + 200L * 25000 / 1000);
    }

    @Test
    void priceUsage_anthropicCacheWriteShape_isDisjoint() {
        int model = seedModel();
        int provider = seedCloudProvider("anthropic");
        seedPrice(model, provider, "billed_input_uncached", "token", 0, "default", 20000, "2020-01-01");
        seedPrice(model, provider, "billed_input_cache_write", "token", 0, "default", 25000, "2020-01-01");

        Long cost = price(model, provider, "2026-01-01", null,
            "{\"prompt_tokens\":1000,\"prompt_cache_write_tokens\":200}");
        assertThat(cost).isEqualTo(1000L * 20000 / 1000 + 200L * 25000 / 1000);
    }

    @Test
    void priceUsage_anthropicDisjoint_neverNegative_chargesCacheWrite() {
        int model = seedModel();
        int provider = seedCloudProvider("anthropic");
        seedPrice(model, provider, "billed_input_uncached", "token", 0, "default", 30000, "2020-01-01");
        seedPrice(model, provider, "billed_input_cache_read", "token", 0, "default", 3000, "2020-01-01");
        seedPrice(model, provider, "billed_input_cache_write", "token", 0, "default", 37500, "2020-01-01");
        seedPrice(model, provider, "billed_input_cache_write_1h", "token", 0, "default", 60000, "2020-01-01");
        seedPrice(model, provider, "billed_output_text", "token", 0, "default", 150000, "2020-01-01");

        Long cost = price(model, provider, "2026-01-01", null,
            "{\"prompt_tokens\":200,\"prompt_cached_tokens\":800,"
            + "\"prompt_cache_write_tokens\":100,\"prompt_cache_write_1h_tokens\":50,"
            + "\"completion_tokens\":400}");
        assertThat(cost).isEqualTo(6000L + 2400L + 3750L + 3000L + 60000L);
    }

    @Test
    void priceUsage_bedrockNativeShape_isDisjointDespiteProviderType() {
        int model = seedModel();
        int provider = seedCloudProvider("bedrock");
        seedPrice(model, provider, "billed_input_uncached", "token", 0, "default", 30000, "2020-01-01");
        seedPrice(model, provider, "billed_input_cache_read", "token", 0, "default", 3000, "2020-01-01");
        seedPrice(model, provider, "billed_input_cache_write", "token", 0, "default", 37500, "2020-01-01");

        Long cost = price(model, provider, "2026-01-01", null,
            "{\"prompt_tokens\":200,\"prompt_cached_tokens\":800,"
            + "\"prompt_cache_write_tokens\":100}");
        assertThat(cost).isEqualTo(200L * 30000 / 1000 + 800L * 3000 / 1000
            + 100L * 37500 / 1000);
    }

    @Test
    void priceUsage_bedrockReadOnlyCacheShape_isDisjoint() {
        int model = seedModel();
        int provider = seedCloudProvider("bedrock");
        seedPrice(model, provider, "billed_input_uncached", "token", 0, "default", 30000, "2020-01-01");
        seedPrice(model, provider, "billed_input_cache_read", "token", 0, "default", 3000, "2020-01-01");

        Long cost = price(model, provider, "2026-01-01", null,
            "{\"prompt_tokens\":200,\"prompt_cached_tokens\":800}");
        assertThat(cost).isEqualTo(200L * 30000 / 1000 + 800L * 3000 / 1000);
    }

    @Test
    void priceUsage_nativeReadOnlyCacheShape_flag_makesItDisjoint() {
        // Native Anthropic cache-read-only turn: cached (100) does not exceed the
        // uncached remainder (1000) and no cache-write count is present, so the
        // usage_shape_disjoint flag the orchestrator sets is the only proof the
        // shape is disjoint. Without it this was billed as 900 uncached + 100
        // read (a bounded undercharge); with it, 1000 uncached + 100 read.
        int model = seedModel();
        int provider = seedCloudProvider("anthropic");
        seedPrice(model, provider, "billed_input_uncached", "token", 0, "default", 20000, "2020-01-01");
        seedPrice(model, provider, "billed_input_cache_read", "token", 0, "default", 2000, "2020-01-01");

        Long cost = price(model, provider, "2026-01-01", null,
            "{\"prompt_tokens\":1000,\"prompt_cached_tokens\":100,\"usage_shape_disjoint\":1}");
        assertThat(cost).isEqualTo(1000L * 20000 / 1000 + 100L * 2000 / 1000);
    }

    @Test
    void priceUsage_disjointFlagIsAuthoritativeWhenProviderTypeIsWrong() {
        // An Anthropic model reached through a custom gateway whose provider row
        // is typed 'openai' (or has no cloud_provider_type). The orchestrator
        // still parsed the native cache spelling and set usage_shape_disjoint;
        // the flag alone must drive the disjoint decomposition, so the small
        // cache-read-only turn is not undercharged.
        int model = seedModel();
        int provider = seedCloudProvider("openai");
        seedPrice(model, provider, "billed_input_uncached", "token", 0, "default", 20000, "2020-01-01");
        seedPrice(model, provider, "billed_input_cache_read", "token", 0, "default", 2000, "2020-01-01");

        Long cost = price(model, provider, "2026-01-01", null,
            "{\"prompt_tokens\":1000,\"prompt_cached_tokens\":100,\"usage_shape_disjoint\":1}");
        assertThat(cost).isEqualTo(1000L * 20000 / 1000 + 100L * 2000 / 1000);
    }

    @Test
    void priceUsage_ocrCreditsSuppressedWhenPagesArePriced() {
        // OCR is derived as both billed_ocr_pages and billed_ocr_credits (one
        // credit per page). A catalogue that prices pages must not also be
        // charged the identical credit count.
        int model = seedModel();
        int provider = seedCloudProvider("openai");
        seedPrice(model, provider, "billed_ocr_pages", "page", 0, "default", 1000, "2020-01-01");
        seedPrice(model, provider, "billed_ocr_credits", "credit", 0, "default", 1000, "2020-01-01");

        Long cost = price(model, provider, "2026-01-01", null,
            "{\"billed_ocr_pages\":3,\"billed_ocr_credits\":3}");
        assertThat(cost).isEqualTo(3L * 1000 / 1000);
    }

    @Test
    void priceUsage_ocrCreditsChargedWhenOnlyCreditPriceExists() {
        int model = seedModel();
        int provider = seedCloudProvider("openai");
        seedPrice(model, provider, "billed_ocr_credits", "credit", 0, "default", 1000, "2020-01-01");

        Long cost = price(model, provider, "2026-01-01", null,
            "{\"billed_ocr_pages\":3,\"billed_ocr_credits\":3}");
        assertThat(cost).isEqualTo(3L * 1000 / 1000);
    }

    @Test
    void priceUsage_anthropicViaOpenAiCompatibleSurface_appliesCacheDiscountOnce() {
        // cloud_provider_type is 'anthropic', but the model is reached through an
        // OpenAI-compatible gateway: prompt_tokens is inclusive (contains the
        // cached tokens) and there is no cache-write counter. The decomposition
        // must not treat this as the disjoint native shape -- doing so bills the
        // cached tokens at the full input rate AND the cache-read rate ( B1).
        int model = seedModel();
        int provider = seedCloudProvider("anthropic");
        seedPrice(model, provider, "billed_input_uncached", "token", 0, "default", 20000, "2020-01-01");
        seedPrice(model, provider, "billed_input_cache_read", "token", 0, "default", 2000, "2020-01-01");

        Long cost = price(model, provider, "2026-01-01", null,
            "{\"prompt_tokens\":1000,\"prompt_cached_tokens\":800}");
        // 200 uncached + 800 cache-read, not 1000 uncached + 800 cache-read.
        assertThat(cost).isEqualTo(200L * 20000 / 1000 + 800L * 2000 / 1000);
    }

    @Test
    void priceUsage_reasoningWithoutOwnPrice_billedOnceAtOutputRate() {
        int model = seedModel();
        int provider = seedCloudProvider("openai");
        seedPrice(model, provider, "billed_input_uncached", "token", 0, "default", 20000, "2020-01-01");
        seedPrice(model, provider, "billed_output_text", "token", 0, "default", 120000, "2020-01-01");

        Long cost = price(model, provider, "2026-01-01", null,
            "{\"prompt_tokens\":832,\"completion_tokens\":319,\"completion_reasoning_tokens\":170}");
        assertThat(cost).isEqualTo(16640L + 38280L);
    }

    @Test
    void priceUsage_perCharacterModel_ignoresTokenQuantities() {
        int model = seedModel();
        int provider = seedCloudProvider("gemini");
        seedPrice(model, provider, "billed_input_characters", "character", 0, "default", 125, "2020-01-01");
        seedPrice(model, provider, "billed_output_characters", "character", 0, "default", 375, "2020-01-01");

        Long cost = price(model, provider, "2026-01-01", null,
            "{\"prompt_tokens\":1000,\"completion_tokens\":500,"
            + "\"billed_input_characters\":4000,\"billed_output_characters\":2000}");
        assertThat(cost).isEqualTo(4000L * 125 / 1000 + 2000L * 375 / 1000);
    }

    @Test
    void priceUsage_suppressesCharacterFallbackPerDirection() {
        int model = seedModel();
        int provider = seedCloudProvider("gemini");
        seedPrice(model, provider, "billed_input_uncached", "token", 0, "default", 20000, "2020-01-01");
        seedPrice(model, provider, "billed_output_characters", "character", 0, "default", 375, "2020-01-01");

        Long cost = price(model, provider, "2026-01-01", null,
            "{\"prompt_tokens\":1000,\"completion_tokens\":0,"
            + "\"billed_input_characters\":4000,\"billed_output_characters\":2000}");
        assertThat(cost).isEqualTo(1000L * 20000 / 1000 + 2000L * 375 / 1000);
    }

    @Test
    void priceUsage_keepsInputAndOutputPixelsDistinct() {
        int model = seedModel();
        int provider = seedCloudProvider("openai");
        seedPrice(model, provider, "billed_input_pixels", "pixel", 0, "default", 100, "2020-01-01");
        seedPrice(model, provider, "billed_output_pixels", "pixel", 0, "default", 300, "2020-01-01");

        Long cost = price(model, provider, "2026-01-01", null,
            "{\"billed_input_pixels\":2000,\"billed_output_pixels\":4000}");
        assertThat(cost).isEqualTo(2000L * 100 / 1000 + 4000L * 300 / 1000);
    }

    @Test
    void priceUsage_decomposesImageTokensAndCachedAudio() {
        int model = seedModel();
        int provider = seedCloudProvider("openai");
        seedPrice(model, provider, "billed_input_uncached", "token", 0, "default", 20000, "2020-01-01");
        seedPrice(model, provider, "billed_input_audio_cache_read", "token", 0, "default", 1000, "2020-01-01");
        seedPrice(model, provider, "billed_input_image_tokens", "token", 0, "default", 30000, "2020-01-01");
        seedPrice(model, provider, "billed_output_text", "token", 0, "default", 120000, "2020-01-01");
        seedPrice(model, provider, "billed_output_image_tokens", "token", 0, "default", 150000, "2020-01-01");

        Long cost = price(model, provider, "2026-01-01", null,
            "{\"prompt_tokens\":1000,\"prompt_cached_tokens\":100,\"prompt_audio_tokens\":100,"
            + "\"prompt_cache_read_audio_tokens\":100,\"prompt_image_tokens\":200,"
            + "\"completion_tokens\":500,\"completion_image_tokens\":100}");
        assertThat(cost).isEqualTo(
            700L * 20000 / 1000 + 100L * 1000 / 1000 + 200L * 30000 / 1000
            + 400L * 120000 / 1000 + 100L * 150000 / 1000);
    }

    @Test
    void priceUsage_imagePricedPerToken_suppressesPixelAndFlatImageForTheSameImage() {
        int model = seedModel();
        int provider = seedCloudProvider("gemini");
        seedPrice(model, provider, "billed_input_image_tokens", "token", 0, "default", 30000, "2020-01-01");
        seedPrice(model, provider, "billed_input_pixels", "pixel", 0, "default", 100, "2020-01-01");
        seedPrice(model, provider, "billed_input_images", "image", 0, "default", 40_000_000, "2020-01-01");

        Long cost = price(model, provider, "2026-01-01", null,
            "{\"prompt_image_tokens\":200,\"billed_input_pixels\":500000,\"billed_input_images\":2}");
        assertThat(cost).isEqualTo(200L * 30000 / 1000);
    }

    @Test
    void priceUsage_imagePricedPerPixel_suppressesFlatImageForTheSameImage() {
        int model = seedModel();
        int provider = seedCloudProvider("openai");
        seedPrice(model, provider, "billed_input_pixels", "pixel", 0, "default", 100, "2020-01-01");
        seedPrice(model, provider, "billed_input_images", "image", 0, "default", 40_000_000, "2020-01-01");

        Long cost = price(model, provider, "2026-01-01", null,
            "{\"billed_input_pixels\":500000,\"billed_input_images\":2}");
        assertThat(cost).isEqualTo(500000L * 100 / 1000);
    }

    @Test
    void priceUsage_flatImagePriceStillChargesWhenItIsTheOnlyImagePrice() {
        int model = seedModel();
        int provider = seedCloudProvider("openai");
        seedPrice(model, provider, "billed_input_images", "image", 0, "default", 40_000_000, "2020-01-01");

        Long cost = price(model, provider, "2026-01-01", null, "{\"billed_input_images\":2}");
        assertThat(cost).isEqualTo(2L * 40_000_000 / 1000);
    }

    @Test
    void priceUsage_pricesOutputImagesAsTheirOwnDimension() {
        int model = seedModel();
        int provider = seedCloudProvider("openai");
        seedPrice(model, provider, "billed_output_images", "image", 0, "default", 40_000_000, "2020-01-01");

        Long cost = price(model, provider, "2026-01-01", null,
            "{\"billed_output_images\":2}");
        assertThat(cost).isEqualTo(2L * 40_000_000 / 1000);
    }

    @Test
    void priceUsage_audioSpeechModel_billedOnRequestCharacters() {
        // Voxtral TTS: catalogue carries only output_cost_per_character, which
        // PriceUpdaterService routes to billed_input_characters for audio_speech
        // models. The audio response has no text, so without that routing the
        // request would be entirely unpriced.
        int model = seedModel();
        int provider = seedCloudProvider("openai");
        seedPrice(model, provider, "billed_input_characters", "character", 0, "default", 1000, "2020-01-01");

        Long cost = price(model, provider, "2026-01-01", null,
            "{\"billed_requests\":1,\"billed_input_characters\":5000,"
            + "\"billed_output_milliseconds\":3000}");
        assertThat(cost).isEqualTo(5000L * 1000 / 1000);
    }

    @Test
    void priceUsage_outputImageTokensSuppressPixelAndFlatRepresentations() {
        int model = seedModel();
        int provider = seedCloudProvider("azure");
        seedPrice(model, provider, "billed_output_image_tokens", "token", 0, "default", 150000, "2020-01-01");
        seedPrice(model, provider, "billed_output_pixels", "pixel", 0, "default", 300, "2020-01-01");
        seedPrice(model, provider, "billed_output_images", "image", 0, "default", 40_000_000, "2020-01-01");

        Long cost = price(model, provider, "2026-01-01", null,
            "{\"completion_tokens\":100,\"completion_image_tokens\":100,"
            + "\"billed_output_pixels\":1048576,\"billed_output_images\":1}");
        assertThat(cost).isEqualTo(100L * 150000 / 1000);
    }

    @Test
    void priceUsage_outputPixelsSuppressFlatImageRepresentation() {
        int model = seedModel();
        int provider = seedCloudProvider("azure");
        seedPrice(model, provider, "billed_output_pixels", "pixel", 0, "default", 300, "2020-01-01");
        seedPrice(model, provider, "billed_output_images", "image", 0, "default", 40_000_000, "2020-01-01");

        Long cost = price(model, provider, "2026-01-01", null,
            "{\"billed_output_pixels\":1048576,\"billed_output_images\":1}");
        assertThat(cost).isEqualTo(Math.round(1048576L * 300 / 1000.0));
    }

    @Test
    void priceUsage_pixelPriceSurvivesABaseInputTokenPrice() {
        // billed_input_image_tokens falls back to billed_input_uncached, so a
        // model with an ordinary input-token price must NOT have its pixel count
        // zeroed when the payload carries no prompt_image_tokens.
        int model = seedModel();
        int provider = seedCloudProvider("openai");
        seedPrice(model, provider, "billed_input_uncached", "token", 0, "default", 20000, "2020-01-01");
        seedPrice(model, provider, "billed_input_pixels", "pixel", 0, "default", 100, "2020-01-01");
        seedPrice(model, provider, "billed_input_images", "image", 0, "default", 40_000_000, "2020-01-01");

        Long cost = price(model, provider, "2026-01-01", null,
            "{\"prompt_tokens\":1000,"
            + "\"billed_input_pixels\":500000,\"billed_input_images\":2}");
        // text tokens + pixels; flat image count stays suppressed by pixels.
        assertThat(cost).isEqualTo(1000L * 20000 / 1000 + 500000L * 100 / 1000);
    }

    @Test
    void priceUsage_outputPixelPriceSurvivesABaseOutputTokenPrice() {
        int model = seedModel();
        int provider = seedCloudProvider("azure");
        seedPrice(model, provider, "billed_output_text", "token", 0, "default", 120000, "2020-01-01");
        seedPrice(model, provider, "billed_output_pixels", "pixel", 0, "default", 300, "2020-01-01");
        seedPrice(model, provider, "billed_output_images", "image", 0, "default", 40_000_000, "2020-01-01");

        Long cost = price(model, provider, "2026-01-01", null,
            "{\"completion_tokens\":100,\"billed_output_pixels\":1048576,\"billed_output_images\":1}");
        assertThat(cost).isEqualTo(100L * 120000 / 1000 + Math.round(1048576L * 300 / 1000.0));
    }

    @Test
    void priceUsage_flatImagePriceSurvivesABaseInputTokenPrice() {
        int model = seedModel();
        int provider = seedCloudProvider("openai");
        seedPrice(model, provider, "billed_input_uncached", "token", 0, "default", 20000, "2020-01-01");
        seedPrice(model, provider, "billed_input_images", "image", 0, "default", 40_000_000, "2020-01-01");

        Long cost = price(model, provider, "2026-01-01", null,
            "{\"prompt_tokens\":1000,\"billed_input_images\":2}");
        assertThat(cost).isEqualTo(1000L * 20000 / 1000 + 2L * 40_000_000 / 1000);
    }

    @Test
    void priceUsage_flatRequestFee_alwaysApplies() {
        int model = seedModel();
        int provider = seedCloudProvider("openai");
        seedPrice(model, provider, "billed_input_uncached", "token", 0, "default", 20000, "2020-01-01");
        seedPrice(model, provider, "billed_output_text", "token", 0, "default", 120000, "2020-01-01");
        seedPrice(model, provider, "billed_requests", "request", 0, "default", 5_000_000, "2020-01-01");

        Long cost = price(model, provider, "2026-01-01", null,
            "{\"prompt_tokens\":10,\"completion_tokens\":10,\"billed_requests\":1}");
        assertThat(cost).isEqualTo(10L * 20000 / 1000 + 10L * 120000 / 1000 + 1L * 5_000_000 / 1000);
    }

    @Test
    void priceUsage_localProviderReturnsNull_andCloudWithoutPricesReturnsNull() {
        int model = seedModel();
        int local = seedLocalProvider();
        assertThat(price(model, local, "2026-01-01", null,
            "{\"prompt_tokens\":10,\"completion_tokens\":10}")).isNull();

        int cloudNoPrices = seedCloudProvider("openai");
        assertThat(price(model, cloudNoPrices, "2026-01-01", null,
            "{\"prompt_tokens\":10,\"completion_tokens\":10}")).isNull();
    }

    @Test
    void priceUsage_citationTokensAreAnIndependentDimension() {
        int model = seedModel();
        int provider = seedCloudProvider("openai");
        seedPrice(model, provider, "billed_citation_tokens", "token", 0, "default", 7000, "2020-01-01");
        assertThat(price(model, provider, "2026-01-01", null,
            "{\"citation_tokens\":300}")).isEqualTo(2100L);
    }

    @Test
    void priceUsage_usesOnlyHighestApplicableVideoDurationTier() {
        int model = seedModel();
        int provider = seedCloudProvider("gemini");
        seedPrice(model, provider, "billed_input_video_milliseconds", "millisecond", 0, "default", 100, "2020-01-01");
        seedPrice(model, provider, "billed_input_video_milliseconds_above_8s", "millisecond", 0, "default", 200, "2020-01-01");
        seedPrice(model, provider, "billed_input_video_milliseconds_above_15s", "millisecond", 0, "default", 300, "2020-01-01");
        assertThat(price(model, provider, "2026-01-01", null,
            "{\"billed_input_video_milliseconds\":16000,"
            + "\"billed_input_video_milliseconds_above_8s\":16000,"
            + "\"billed_input_video_milliseconds_above_15s\":16000}"))
            .isEqualTo(16000L * 300 / 1000);
    }

    @Test
    void priceUsage_perPromptSearchDoesNotAlsoChargeQueries() {
        int model = seedModel();
        int provider = seedCloudProvider("gemini");
        seedPrice(model, provider, "billed_search_prompts", "request", 0, "default", 5000, "2020-01-01");
        assertThat(price(model, provider, "2026-01-01", null,
            "{\"billed_search_queries\":3,\"billed_search_prompts\":1}"))
            .isEqualTo(5L);
    }

    @Test
    void priceUsage_highContextSearchFallsBackToScalarSearchPrice() {
        // The catalogue carries only the scalar search-query price; a request that
        // asked for a high context size still bills at that rate rather than free.
        int model = seedModel();
        int provider = seedCloudProvider("openai");
        seedPrice(model, provider, "billed_search_queries", "query", 0, "default", 1_000_000, "2020-01-01");
        assertThat(price(model, provider, "2026-01-01", null, "{\"billed_search_queries_high\":3}"))
            .isEqualTo(3L * 1_000_000 / 1000);
    }

    @Test
    void priceUsage_sizeSpecificSearchPriceStillWinsWhenPresent() {
        int model = seedModel();
        int provider = seedCloudProvider("openai");
        seedPrice(model, provider, "billed_search_queries", "query", 0, "default", 1_000_000, "2020-01-01");
        seedPrice(model, provider, "billed_search_queries_high", "query", 0, "default", 3_000_000, "2020-01-01");
        assertThat(price(model, provider, "2026-01-01", null, "{\"billed_search_queries_high\":2}"))
            .isEqualTo(2L * 3_000_000 / 1000);
    }

    @Test
    void priceUsage_highContextSearchPromptFallsBackToScalarPromptPrice() {
        int model = seedModel();
        int provider = seedCloudProvider("openai");
        seedPrice(model, provider, "billed_search_prompts", "request", 0, "default", 25_000_000, "2020-01-01");
        assertThat(price(model, provider, "2026-01-01", null, "{\"billed_search_prompts_high\":1}"))
            .isEqualTo(25_000_000L / 1000);
    }

    // --------------------------------------------------------------- views

    @Test
    void budgetUsageAndLogEntryCost_agreeAndUseTheFunction() {
        int model = seedModel();
        int provider = seedCloudProvider("openai");
        seedPrice(model, provider, "billed_input_uncached", "token", 0, "default", 20000, "2020-01-01");
        seedPrice(model, provider, "billed_output_text", "token", 0, "default", 120000, "2020-01-01");
        int apiKey = seedApiKey(seedTeam());
        int le = seedLogEntry(apiKey, model, provider, "2026-05-10T12:00:00Z");
        // usage_tokens holds the raw extract_token_usage vocabulary, not the
        // pre-decomposed billed_* quantities — logos_price_usage decomposes.
        seedUsageToken(le, "prompt_tokens", 1000);
        seedUsageToken(le, "completion_tokens", 200);

        Long viaLec = jdbc.queryForObject(
            "SELECT cost_micro_cents FROM log_entry_cost WHERE log_entry_id = ?", Long.class, le);
        Long viaBudget = jdbc.queryForObject(
            "SELECT cost_micro_cents FROM budget_usage WHERE api_key_id = ? AND month = DATE '2026-05-01'",
            Long.class, apiKey);
        assertThat(viaLec).isEqualTo(1000L * 20000 / 1000 + 200L * 120000 / 1000);
        assertThat(viaBudget).isEqualTo(viaLec);
    }

    @Test
    void logEntryCost_failedRequest_billsDeliveredOutputButNotTheRequestFee() {
        int model = seedModel();
        int provider = seedCloudProvider("openai");
        // The provider still billed Logos for the 40 output tokens the stream
        // produced before it dropped; the flat per-request fee is not owed.
        seedPrice(model, provider, "billed_requests", "request", 0, "default", 5000, "2020-01-01");
        seedPrice(model, provider, "billed_output_text", "token", 0, "default", 120000, "2020-01-01");
        int apiKey = seedApiKey(seedTeam());
        int le = seedLogEntry(apiKey, model, provider, "2026-05-10T12:00:00Z", "error");
        seedUsageToken(le, "billed_requests", 1);
        seedUsageToken(le, "completion_tokens", 40);

        Long viaLec = jdbc.queryForObject(
            "SELECT cost_micro_cents FROM log_entry_cost WHERE log_entry_id = ?", Long.class, le);
        assertThat(viaLec).isEqualTo(40L * 120000 / 1000);
    }

    @Test
    void logEntryCost_failedRequest_doesNotBillRequestDerivedInputEstimates() {
        int model = seedModel();
        int provider = seedCloudProvider("gemini");
        // A character-priced model whose request text was measured but rejected:
        // billed_input_characters describes what the caller sent, not consumption.
        seedPrice(model, provider, "billed_input_characters", "character", 0, "default", 100, "2020-01-01");
        int apiKey = seedApiKey(seedTeam());
        int le = seedLogEntry(apiKey, model, provider, "2026-05-10T12:00:00Z", "error");
        seedUsageToken(le, "billed_input_characters", 4000);

        Long viaLec = jdbc.queryForObject(
            "SELECT cost_micro_cents FROM log_entry_cost WHERE log_entry_id = ?", Long.class, le);
        assertThat(viaLec).isNull();
    }

    @Test
    void logEntryCost_failedGeneration_doesNotBillTheRequestedImageOrDurationCount() {
        int model = seedModel();
        int provider = seedCloudProvider("openai");
        // derive_billable_quantities falls back to the requested n / duration when
        // the response has no data; a rejected generation delivered none of it.
        seedPrice(model, provider, "billed_output_images", "image", 0, "default", 40_000_000, "2020-01-01");
        seedPrice(model, provider, "billed_output_pixels", "pixel", 0, "default", 1, "2020-01-01");
        seedPrice(model, provider, "billed_output_milliseconds", "millisecond", 0, "default", 200, "2020-01-01");
        int apiKey = seedApiKey(seedTeam());
        int le = seedLogEntry(apiKey, model, provider, "2026-05-10T12:00:00Z", "error");
        seedUsageToken(le, "billed_output_images", 4);
        seedUsageToken(le, "billed_output_pixels", 4 * 1024 * 1024);
        seedUsageToken(le, "billed_output_milliseconds", 8000);

        Long viaLec = jdbc.queryForObject(
            "SELECT cost_micro_cents FROM log_entry_cost WHERE log_entry_id = ?", Long.class, le);
        assertThat(viaLec).isNull();
    }

    @Test
    void logEntryCost_failedGeneration_doesNotBillTieredInputVideoDuration() {
        int model = seedModel();
        int provider = seedCloudProvider("gemini");
        seedPrice(model, provider, "billed_input_video_milliseconds_above_8s", "millisecond", 0, "default", 200, "2020-01-01");
        seedPrice(model, provider, "billed_input_video_milliseconds_above_15s", "millisecond", 0, "default", 300, "2020-01-01");
        int apiKey = seedApiKey(seedTeam());
        int le = seedLogEntry(apiKey, model, provider, "2026-05-10T12:00:00Z", "error");
        seedUsageToken(le, "billed_input_video_milliseconds_above_8s", 16_000);
        seedUsageToken(le, "billed_input_video_milliseconds_above_15s", 16_000);

        Long viaLec = jdbc.queryForObject(
            "SELECT cost_micro_cents FROM log_entry_cost WHERE log_entry_id = ?", Long.class, le);
        assertThat(viaLec).isNull();
    }

    @Test
    void logEntryCost_inFlightRequestIsNotPricedUntilItFinishes() {
        int model = seedModel();
        int provider = seedCloudProvider("openai");
        seedPrice(model, provider, "billed_output_text", "token", 0, "default", 120000, "2020-01-01");
        int apiKey = seedApiKey(seedTeam());
        int le = seedLogEntry(apiKey, model, provider, "2026-05-10T12:00:00Z", (String) null);
        seedUsageToken(le, "completion_tokens", 40);

        Long viaLec = jdbc.queryForObject(
            "SELECT cost_micro_cents FROM log_entry_cost WHERE log_entry_id = ?", Long.class, le);
        assertThat(viaLec).isNull();
    }

    @Test
    void tokenPriceGapsMapsRawUsageToCanonicalQuantity() {
        int model = seedModel();
        int provider = seedCloudProvider("openai");
        int le = seedLogEntry(seedApiKey(seedTeam()), model, provider, "2026-05-10T12:00:00Z");
        seedUsageToken(le, "prompt_tokens", 321);

        Integer gaps = jdbc.queryForObject(
            "SELECT COUNT(*) FROM token_price_gaps WHERE model_id = ? AND provider_id = ? "
            + "AND quantity = 'billed_input_uncached' AND tokens = 321",
            Integer.class, model, provider);
        assertThat(gaps).isEqualTo(1);
    }

    @Test
    void tokenPriceGaps_ignoresCharactersSuppressedByTokenPricing() {
        int model = seedModel();
        int provider = seedCloudProvider("openai");
        seedPrice(model, provider, "billed_input_uncached", "token", 0, "default", 20000, "2020-01-01");
        int le = seedLogEntry(seedApiKey(seedTeam()), model, provider, "2026-05-10T12:00:00Z");
        seedUsageToken(le, "billed_input_characters", 321);

        Integer gaps = jdbc.queryForObject(
            "SELECT COUNT(*) FROM token_price_gaps WHERE model_id = ? AND provider_id = ? "
            + "AND quantity = 'billed_input_characters'",
            Integer.class, model, provider);
        assertThat(gaps).isZero();
    }

    // ------------------------------------------------------ price history

    @Test
    void collapsePriceSentinelsRepricesSyntheticHistoryFromEarliestCorrection() {
        int model = seedModel();
        int provider = seedCloudProvider("openai");
        seedPrice(model, provider, "billed_input_uncached", "token", 0, "default", 100000, "2020-01-01");
        seedPrice(model, provider, "billed_input_uncached", "token", 0, "default", 20000, "2026-08-05");

        Integer changed = jdbc.queryForObject(
            "SELECT logos_collapse_price_sentinels()", Integer.class);

        Integer rows = jdbc.queryForObject(
            "SELECT COUNT(*) FROM token_prices tp JOIN token_types t ON t.id = tp.type_id "
            + "WHERE tp.model_id = ? AND t.name = 'billed_input_uncached'", Integer.class, model);
        assertThat(changed).isEqualTo(1);
        assertThat(rows).isEqualTo(1);
        Long historical = resolve("billed_input_uncached", model, provider, "2021-01-01", 1L, null);
        Long current = resolve("billed_input_uncached", model, provider, "2026-09-01", 1L, null);
        assertThat(historical).isEqualTo(20000L);
        assertThat(current).isEqualTo(20000L);

        assertThat(jdbc.queryForObject(
            "SELECT logos_collapse_price_sentinels()", Integer.class)).isZero();
    }

    // ------------------------------------------- legacy spelling normalization
    //
    // The 022-8 changelog runs on a fresh Testcontainer before any legacy-spelling
    // row exists, so these seed the pre-canonical rows and invoke the same
    // normalization function the changelog calls, then assert on the result.

    @Test
    void normalizeLegacyTokenSpellings_foldsBareAndUnprefixedNamesOntoCanonical() {
        int model = seedModel();
        int provider = seedCloudProvider("openai");
        seedPrice(model, provider, "billed_input_uncached", "token", 0, "default", 20000, "2020-01-01");
        seedPrice(model, provider, "billed_input_cache_read", "token", 0, "default", 2000, "2020-01-01");
        seedPrice(model, provider, "billed_output_text", "token", 0, "default", 120000, "2020-01-01");
        // A database old enough to carry the legacy spellings has since moved to
        // the canonical names for newer traffic, so those types already exist.
        int newer = seedLogEntry(seedApiKey(seedTeam()), model, provider, "2026-08-01T12:00:00Z");
        seedUsageToken(newer, "prompt_tokens", 10);
        seedUsageToken(newer, "completion_tokens", 10);

        int le = seedLogEntry(seedApiKey(seedTeam()), model, provider, "2026-05-10T12:00:00Z");
        seedUsageToken(le, "prompt", 1000);
        seedUsageToken(le, "completion", 200);
        seedUsageToken(le, "cached_tokens", 800);

        // Before: logos_price_usage reads none of these -> no pricing knowledge.
        assertThat(jdbc.queryForObject(
            "SELECT cost_micro_cents FROM log_entry_cost WHERE log_entry_id = ?", Long.class, le))
            .isNull();

        jdbc.queryForObject("SELECT logos_normalize_legacy_token_spellings()", Integer.class);

        assertThat(jdbc.queryForObject(
            "SELECT COUNT(*) FROM usage_tokens ut JOIN token_types tt ON tt.id = ut.type_id "
            + "WHERE tt.name IN ('prompt','completion','cached_tokens')", Integer.class)).isZero();

        // After: priced as an inclusive OpenAI response --
        // 200 uncached + 800 cache-read + 200 output.
        Long cost = jdbc.queryForObject(
            "SELECT logos_price_usage(?, ?, ?::timestamptz, NULL, "
            + "(SELECT jsonb_object_agg(tt.name, ut.token_count) FROM usage_tokens ut "
            + " JOIN token_types tt ON tt.id = ut.type_id WHERE ut.log_entry_id = ?))",
            Long.class, model, provider, "2026-05-10T12:00:00Z", le);
        assertThat(cost).isEqualTo(200L * 20000 / 1000 + 800L * 2000 / 1000 + 200L * 120000 / 1000);
    }

    @Test
    void normalizeLegacyTokenSpellings_mergesIntoExistingCanonicalRowByGreatest() {
        int model = seedModel();
        int provider = seedCloudProvider("openai");
        int le = seedLogEntry(seedApiKey(seedTeam()), model, provider, "2026-05-10T12:00:00Z");
        seedUsageToken(le, "prompt_tokens", 900);
        seedUsageToken(le, "prompt", 1000);

        jdbc.queryForObject("SELECT logos_normalize_legacy_token_spellings()", Integer.class);

        Long merged = jdbc.queryForObject(
            "SELECT ut.token_count FROM usage_tokens ut JOIN token_types tt ON tt.id = ut.type_id "
            + "WHERE ut.log_entry_id = ? AND tt.name = 'prompt_tokens'", Long.class, le);
        assertThat(merged).isEqualTo(1000L);
    }

    @Test
    @org.springframework.transaction.annotation.Transactional
    void normalizeLegacyTokenSpellings_preservesRowsWhoseCanonicalTargetIsAbsent() {
        // Data-loss guard: if the canonical type does not exist anywhere, the
        // legacy row cannot be migrated and must be left intact rather than
        // cascade-deleted with its type. The static container is shared and other
        // tests seed the canonical types, so this runs in a rolled-back
        // transaction where those types and their referencing rows are removed to
        // establish the precondition.
        jdbc.update("DELETE FROM usage_tokens ut USING token_types tt WHERE tt.id = ut.type_id "
            + "AND tt.name IN ('prompt_tokens','completion_tokens')");
        jdbc.update("DELETE FROM token_prices tp USING token_types tt WHERE tt.id = tp.type_id "
            + "AND tt.name IN ('prompt_tokens','completion_tokens')");
        jdbc.update("DELETE FROM token_types WHERE name IN ('prompt_tokens','completion_tokens')");

        int model = seedModel();
        int provider = seedCloudProvider("openai");
        int le = seedLogEntry(seedApiKey(seedTeam()), model, provider, "2026-05-10T12:00:00Z");
        seedUsageToken(le, "prompt", 1000);
        seedUsageToken(le, "completion", 200);

        jdbc.queryForObject("SELECT logos_normalize_legacy_token_spellings()", Integer.class);

        // Both the rows and their types survive, untouched.
        assertThat(jdbc.queryForObject(
            "SELECT ut.token_count FROM usage_tokens ut JOIN token_types tt ON tt.id = ut.type_id "
            + "WHERE ut.log_entry_id = ? AND tt.name = 'prompt'", Long.class, le)).isEqualTo(1000L);
        assertThat(jdbc.queryForObject(
            "SELECT COUNT(*) FROM token_types WHERE name IN ('prompt','completion')", Integer.class))
            .isEqualTo(2);
    }

    @Test
    void normalizeLegacyTokenSpellings_repointsLegacyPriceRowUnlessCanonicalExists() {
        int model = seedModel();
        int provider = seedCloudProvider("openai");
        // Legacy 'prompt' price row, no canonical row for this model/tuple -> repointed.
        seedPrice(model, provider, "prompt", "token", 0, "default", 30000, "2020-01-01");
        // Legacy 'completion' collides with an existing canonical row for the tuple -> discarded.
        seedPrice(model, provider, "completion", "token", 0, "default", 99999, "2020-01-01");
        seedPrice(model, provider, "billed_output_text", "token", 0, "default", 120000, "2020-01-01");

        jdbc.queryForObject("SELECT logos_normalize_legacy_token_spellings()", Integer.class);

        assertThat(resolve("billed_input_uncached", model, provider, "2026-01-01", 1L, null))
            .isEqualTo(30000L);
        assertThat(resolve("billed_output_text", model, provider, "2026-01-01", 1L, null))
            .isEqualTo(120000L);
        assertThat(jdbc.queryForObject(
            "SELECT COUNT(*) FROM token_prices tp JOIN token_types tt ON tt.id = tp.type_id "
            + "WHERE tt.name IN ('prompt','completion')", Integer.class)).isZero();
    }

    // --------------------------------------------------------------- helpers

    @Test
    void optimizedPricingMatchesIndependentLegacyFunctions() throws Exception {
        String source = pricingResource("023_token_cost_decomposition.xml");
        String resolver = legacyFunction(source, "logos_resolve_unit_price")
            .replace("logos_resolve_unit_price", "pricing_test_legacy_resolver")
            .replace("LANGUAGE sql STABLE AS", "LANGUAGE sql STABLE SET search_path = public, pg_catalog AS");
        String pricing = legacyFunction(source, "logos_price_usage")
            .replace("logos_price_usage", "pricing_test_legacy_price")
            .replace("logos_resolve_unit_price", "pricing_test_legacy_resolver")
            .replace("LANGUAGE plpgsql STABLE AS", "LANGUAGE plpgsql STABLE SET search_path = public, pg_catalog AS");
        assertThat(resolver).contains("SET search_path = public, pg_catalog", "FUNCTION pricing_test_legacy_resolver(");
        assertThat(pricing).contains("SET search_path = public, pg_catalog", "pricing_test_legacy_resolver(")
            .doesNotContain("logos_resolve_unit_price(");
        jdbc.execute(resolver);
        jdbc.execute(pricing);

        int catalogueModel = seedModel(), fallbackModel = seedModel(), zeroModel = seedModel(), unpricedModel = seedModel();
        int openAi = seedCloudProvider("openai"), anthropic = seedCloudProvider("anthropic");
        int zeroProvider = seedCloudProvider("openai"), local = seedLocalProvider();
        List<String> quantities = jdbc.queryForList(
            "SELECT name FROM token_types WHERE name LIKE 'billed_%' OR name = 'audio_milliseconds' ORDER BY name", String.class);
        int sequence = 1;
        for (String quantity : quantities) {
            String unit = quantity.endsWith("characters") ? "character" : quantity.endsWith("images") ? "image"
                : quantity.endsWith("pixels") ? "pixel" : quantity.contains("milliseconds") ? "millisecond"
                : quantity.endsWith("pages") ? "page" : quantity.endsWith("credits") ? "credit"
                : quantity.contains("queries") ? "query" : quantity.contains("prompts") || quantity.equals("billed_requests") ? "request"
                : quantity.endsWith("sessions") ? "session" : quantity.startsWith("billed_guardrail_") ? "unit" : "token";
            seedPrice(catalogueModel, openAi, quantity, unit, 0, "default", 1000 + sequence++, "2020-01-01");
        }
        seedPrice(fallbackModel, anthropic, "billed_input_uncached", "token", 0, "default", 20000, "2020-01-01");
        seedPrice(fallbackModel, anthropic, "billed_output_text", "token", 0, "default", 120000, "2020-01-01");
        seedPrice(fallbackModel, anthropic, "billed_input_uncached", "token", 200000, "default", 40000, "2020-01-01");
        seedPrice(fallbackModel, anthropic, "billed_input_uncached", "token", 0, "flex", 10000, "2020-01-01");
        seedPrice(fallbackModel, anthropic, "billed_input_uncached", "token", 0, "default", 25000, "2026-08-01");
        seedPrice(zeroModel, zeroProvider, "billed_input_uncached", "token", 0, "default", 0, "2020-01-01");
        seedPrice(zeroModel, zeroProvider, "billed_output_text", "token", 0, "default", 0, "2020-01-01");
        jdbc.update("INSERT INTO token_prices (type_id, model_id, provider_id, unit, min_context_tokens, service_tier, valid_from, price_per_k_unit) "
            + "SELECT id, NULL, ?, 'token', 0, 'default', '2020-01-01'::timestamptz, 30000 FROM token_types WHERE name = 'billed_input_uncached'", openAi);
        jdbc.update("INSERT INTO token_prices (type_id, model_id, provider_id, unit, min_context_tokens, service_tier, valid_from, price_per_k_unit) "
            + "SELECT id, ?, NULL, 'token', 0, 'default', '2020-01-01'::timestamptz, 15000 FROM token_types WHERE name = 'billed_output_text'", catalogueModel);

        record Catalogue(Integer model, Integer provider, String at, String tier) {}
        List<Catalogue> cases = List.of(
            new Catalogue(catalogueModel, openAi, "2026-01-01", null),
            new Catalogue(catalogueModel, openAi, "2026-09-01", "flex"),
            new Catalogue(catalogueModel, anthropic, "2026-01-01", "unknown"),
            new Catalogue(fallbackModel, anthropic, "2026-01-01", null),
            new Catalogue(fallbackModel, anthropic, "2026-09-01", "flex"),
            new Catalogue(zeroModel, zeroProvider, "2026-01-01", null),
            new Catalogue(unpricedModel, anthropic, "2026-01-01", null),
            new Catalogue(catalogueModel, local, "2026-01-01", null),
            new Catalogue(null, openAi, "2026-01-01", null),
            new Catalogue(-1, openAi, "2026-01-01", null),
            new Catalogue(catalogueModel, null, "2026-01-01", null));
        List<String> usages = new ArrayList<>(Arrays.asList(null, "{}", "null", "[]", "\"billed_input_characters\"",
            "[\"billed_input_characters\"]", "{\"prompt_tokens\":1000,\"completion_tokens\":300}",
            "{\"prompt_tokens\":300000,\"completion_tokens\":300}",
            "{\"prompt_tokens\":1000,\"prompt_cached_tokens\":800,\"completion_tokens\":300}",
            "{\"prompt_tokens\":200,\"prompt_cached_tokens\":800,\"prompt_cache_write_tokens\":20,\"usage_shape_disjoint\":1}",
            "{\"prompt_tokens\":1000,\"prompt_cache_miss_tokens\":200,\"prompt_cached_tokens\":800}",
            "{\"prompt_tokens\":1000,\"prompt_audio_tokens\":300,\"prompt_cache_read_audio_tokens\":100,\"completion_audio_tokens\":20}",
            "{\"prompt_tokens\":1000,\"prompt_image_tokens\":200,\"billed_input_pixels\":5000,\"billed_input_images\":2}",
            "{\"completion_tokens\":1000,\"completion_reasoning_tokens\":300,\"completion_image_tokens\":200,\"completion_video_tokens\":100}",
            "{\"billed_input_video_milliseconds\":20000,\"billed_input_video_milliseconds_above_8s\":12000,\"billed_input_video_milliseconds_above_15s\":5000}",
            "{\"billed_ocr_pages\":2,\"billed_ocr_credits\":2}", "{\"billed_ocr_credits\":2}",
            "{\"billed_input_characters\":1000,\"billed_output_characters\":300}",
            "{\"prompt_tokens\":100,\"billed_input_characters\":\"bad\",\"billed_output_characters\":\"bad\"}",
            "{\"billed_input_characters\":null,\"billed_output_characters\":0,\"billed_ocr_credits\":-1}",
            "{\"billed_requests\":1,\"billed_search_queries_low\":2,\"billed_code_interpreter_sessions\":1}"));
        for (String quantity : quantities) usages.add("{\"" + quantity + "\":1000}");
        try {
            for (Catalogue c : cases) {
                for (String usage : usages) {
                    assertThat(pricingOutcome("logos_price_usage", c.model(), c.provider(), c.at(), c.tier(), usage))
                        .as("catalogue=%s usage=%s", c, usage)
                        .isEqualTo(pricingOutcome("pricing_test_legacy_price", c.model(), c.provider(), c.at(), c.tier(), usage));
                }
                for (String quantity : quantities) {
                    for (Long context : Arrays.asList(null, 0L, 1000L, 300000L)) {
                        Object[] args = {quantity, c.model(), c.provider(), c.at(), context, c.tier()};
                        assertThat(jdbc.queryForObject("SELECT logos_resolve_unit_price(?::text,?::integer,?::integer,?::timestamptz,?::bigint,?::text)", Long.class, args))
                            .as("catalogue=%s quantity=%s context=%s", c, quantity, context)
                            .isEqualTo(jdbc.queryForObject("SELECT pricing_test_legacy_resolver(?::text,?::integer,?::integer,?::timestamptz,?::bigint,?::text)", Long.class, args));
                    }
                }
            }
        } finally {
            jdbc.execute("DROP FUNCTION pricing_test_legacy_price(integer,integer,timestamptz,text,jsonb)");
            jdbc.execute("DROP FUNCTION pricing_test_legacy_resolver(text,integer,integer,timestamptz,bigint,text)");
            // Other tests exercise catalogue-wide maintenance, so leave no extra price history behind.
            jdbc.update("DELETE FROM token_prices WHERE model_id IN (?,?,?,?) OR provider_id IN (?,?,?,?)",
                catalogueModel, fallbackModel, zeroModel, unpricedModel, openAi, anthropic, zeroProvider, local);
            jdbc.update("DELETE FROM providers WHERE id IN (?,?,?,?)", openAi, anthropic, zeroProvider, local);
            jdbc.update("DELETE FROM models WHERE id IN (?,?,?,?)", catalogueModel, fallbackModel, zeroModel, unpricedModel);
        }
    }

    @Test
    void pricingSearchPathIsPinnedAfterCleanInstall() throws Exception {
        try (Connection connection = jdbc.getDataSource().getConnection()) {
            assertPricingSearchPath(connection);
            assertPreparedPricingResolver(connection);
        }
    }

    @Test
    void reusedPricingPlansReadChangedCatalogueValues() {
        int model = seedModel(), provider = seedCloudProvider("openai");
        seedPrice(model, provider, "billed_input_uncached", "token", 0, "default", 20000, "2020-01-01");
        for (int i = 0; i < 12; i++) {
            assertThat(price(model, provider, "2026-01-01", null, "{\"prompt_tokens\":1000}")).isEqualTo(20000L);
        }
        jdbc.update("UPDATE token_prices SET price_per_k_unit=40000 WHERE model_id=? AND provider_id=?", model, provider);
        assertThat(price(model, provider, "2026-01-01", null, "{\"prompt_tokens\":1000}")).isEqualTo(40000L);
    }

    @Test
    void pricingMigrationUpgradesExistingFunctionsAndRetainsSearchPath(@TempDir Path directory) throws Exception {
        String database = "pricing_upgrade_" + SEQ.getAndIncrement();
        jdbc.execute("CREATE DATABASE " + database);
        String url = postgres.getJdbcUrl().replace("/logosdb", "/" + database);
        try {
            Path oldMaster = directory.resolve("liquibase/changelog/master-before-pricing.xml");
            Files.createDirectories(oldMaster.getParent());
            String master = pricingResource("master.xml").replace(
                "    <include file=\"liquibase/changelog/080_gateway_pricing_performance.xml\"/>\n", "");
            assertThat(master).doesNotContain("080_gateway_pricing_performance");
            Files.writeString(oldMaster, master);
            migratePricingDatabase(url, "liquibase/changelog/master-before-pricing.xml", directory);
            List<String> previousHistory;
            try (Connection connection = DriverManager.getConnection(url, postgres.getUsername(), postgres.getPassword())) {
                assertPricingSearchPath(connection);
                previousHistory = previousMigrationHistory(connection);
            }
            migratePricingDatabase(url, "liquibase/changelog/master.xml", directory);
            try (Connection connection = DriverManager.getConnection(url, postgres.getUsername(), postgres.getPassword())) {
                assertPricingSearchPath(connection);
                assertPreparedPricingResolver(connection);
                assertThat(previousMigrationHistory(connection)).containsExactlyElementsOf(previousHistory);
                try (var rows = connection.createStatement().executeQuery("SELECT COUNT(*) FROM databasechangelog WHERE id LIKE '080-%'")) {
                    assertThat(rows.next()).isTrue();
                    assertThat(rows.getInt(1)).isEqualTo(2);
                }
                try (var rows = connection.createStatement().executeQuery("SELECT prosrc FROM pg_proc "
                        + "WHERE oid='public.logos_price_usage(integer,integer,timestamptz,text,jsonb)'::regprocedure")) {
                    assertThat(rows.next()).isTrue();
                    assertThat(rows.getString(1)).contains("(u ? 'billed_input_characters') AND");
                }
            }
        } finally {
            jdbc.execute("DROP DATABASE " + database + " WITH (FORCE)");
        }
    }

    private static List<String> previousMigrationHistory(Connection connection) throws SQLException {
        List<String> history = new ArrayList<>();
        try (var rows = connection.createStatement().executeQuery("SELECT row_to_json(d)::text FROM databasechangelog d "
                + "WHERE id NOT LIKE '080-%' ORDER BY orderexecuted")) {
            while (rows.next()) history.add(rows.getString(1));
        }
        return history;
    }

    private static String pricingResource(String name) throws Exception {
        try (var in = TokenCostFunctionTest.class.getResourceAsStream("/liquibase/changelog/" + name)) {
            return new String(java.util.Objects.requireNonNull(in).readAllBytes(), StandardCharsets.UTF_8);
        }
    }

    private static String legacyFunction(String source, String name) {
        var match = Pattern.compile("CREATE OR REPLACE FUNCTION " + name + "\\(.*?\\$fn\\$;", Pattern.DOTALL).matcher(source);
        assertThat(match.find()).isTrue();
        return match.group();
    }

    private record PricingOutcome(Long cost, String sqlState) {}

    private PricingOutcome pricingOutcome(String function, Object... args) {
        try {
            return new PricingOutcome(jdbc.queryForObject("SELECT " + function
                + "(?::integer,?::integer,?::timestamptz,?::text,?::jsonb)", Long.class, args), null);
        } catch (org.springframework.dao.DataAccessException error) {
            for (Throwable cause = error; cause != null; cause = cause.getCause()) {
                if (cause instanceof SQLException sql) return new PricingOutcome(null, sql.getSQLState());
            }
            throw error;
        }
    }

    private static void migratePricingDatabase(String url, String master, Path directory) throws Exception {
        try (var accessor = new liquibase.resource.CompositeResourceAccessor(
                new liquibase.resource.DirectoryResourceAccessor(directory), new liquibase.resource.ClassLoaderResourceAccessor());
             Connection connection = DriverManager.getConnection(url, postgres.getUsername(), postgres.getPassword());
             var migration = new liquibase.Liquibase(master, accessor, new liquibase.database.jvm.JdbcConnection(connection))) {
            migration.update(new liquibase.Contexts(), new liquibase.LabelExpression());
        }
    }

    private static void assertPreparedPricingResolver(Connection connection) throws Exception {
        try (var rows = connection.createStatement().executeQuery("SELECT l.lanname FROM pg_proc p "
                + "JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_language l ON l.oid=p.prolang "
                + "WHERE n.nspname='public' AND p.proname='logos_resolve_unit_price'")) {
            assertThat(rows.next()).isTrue();
            assertThat(rows.getString(1)).isEqualTo("plpgsql");
        }
    }

    private static void assertPricingSearchPath(Connection connection) throws Exception {
        for (String name : List.of("logos_price_usage", "logos_resolve_unit_price")) {
            try (var statement = connection.prepareStatement("SELECT array_to_string(p.proconfig, ';') FROM pg_proc p "
                    + "JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.proname=?")) {
                statement.setString(1, name);
                try (var rows = statement.executeQuery()) {
                    assertThat(rows.next()).isTrue();
                    assertThat(rows.getString(1)).contains("search_path=public, pg_catalog");
                }
            }
        }
        boolean autoCommit = connection.getAutoCommit();
        connection.setAutoCommit(false);
        try (var statement = connection.createStatement()) {
            statement.execute("SET LOCAL search_path = pg_catalog");
            try (var rows = statement.executeQuery("SELECT public.logos_price_usage(-1,-1,NOW(),NULL,'{}'::jsonb), "
                    + "public.logos_resolve_unit_price('__pricing_search_path_probe__',-1,-1,NOW(),0,NULL)")) {
                assertThat(rows.next()).isTrue();
                assertThat(rows.getObject(1)).isNull();
                assertThat(rows.getObject(2)).isNull();
            }
        } finally {
            connection.rollback();
            connection.setAutoCommit(autoCommit);
        }
    }

    private Long resolve(String q, int model, int provider, String at, Long ctx, String tier) {
        return jdbc.queryForObject(
            "SELECT logos_resolve_unit_price(?, ?, ?, ?::timestamptz, ?, ?)",
            Long.class, q, model, provider, at, ctx, tier);
    }

    private Long price(int model, int provider, String at, String tier, String usageJson) {
        return jdbc.queryForObject(
            "SELECT logos_price_usage(?, ?, ?::timestamptz, ?, ?::jsonb)",
            Long.class, model, provider, at, tier, usageJson);
    }

    private int seedModel() {
        return jdbc.queryForObject(
            "INSERT INTO models (name) VALUES (?) RETURNING id",
            Integer.class, "m-" + SEQ.getAndIncrement());
    }

    private int seedCloudProvider(String cloudType) {
        return jdbc.queryForObject(
            "INSERT INTO providers (name, base_url, provider_type, cloud_provider_type, auth_name, auth_format) "
            + "VALUES (?, 'http://x', 'cloud', CAST(? AS cloud_provider_type_enum), 'Authorization', 'Bearer %s') "
            + "RETURNING id",
            Integer.class, "p-" + SEQ.getAndIncrement(), cloudType);
    }

    private int seedLocalProvider() {
        return jdbc.queryForObject(
            "INSERT INTO providers (name, base_url, provider_type, auth_name, auth_format) "
            + "VALUES (?, 'http://x', 'logosnode', 'Authorization', 'Bearer %s') RETURNING id",
            Integer.class, "p-" + SEQ.getAndIncrement());
    }

    private int seedTeam() {
        return jdbc.queryForObject(
            "INSERT INTO teams (name) VALUES (?) RETURNING id",
            Integer.class, "t-" + SEQ.getAndIncrement());
    }

    private int seedApiKey(int teamId) {
        return jdbc.queryForObject(
            "INSERT INTO api_keys (key_value, name, team_id) VALUES (?, ?, ?) RETURNING id",
            Integer.class, "lg-" + SEQ.getAndIncrement(), "k-" + SEQ.get(), teamId);
    }

    private int seedLogEntry(int apiKeyId, int modelId, int providerId, String tsRequest) {
        return seedLogEntry(apiKeyId, modelId, providerId, tsRequest, "success");
    }

    private int seedLogEntry(int apiKeyId, int modelId, int providerId, String tsRequest, String resultStatus) {
        return jdbc.queryForObject(
            "INSERT INTO log_entry (timestamp_request, api_key_id, model_id, provider_id, result_status) "
            + "VALUES (?::timestamptz, ?, ?, ?, ?::result_status_enum) RETURNING id",
            Integer.class, tsRequest, apiKeyId, modelId, providerId, resultStatus);
    }

    private void seedUsageToken(int logEntryId, String typeName, long count) {
        jdbc.update("INSERT INTO token_types (name) VALUES (?) ON CONFLICT (name) DO NOTHING", typeName);
        Integer typeId = jdbc.queryForObject(
            "SELECT id FROM token_types WHERE name = ?", Integer.class, typeName);
        jdbc.update("INSERT INTO usage_tokens (type_id, log_entry_id, token_count) VALUES (?, ?, ?)",
            typeId, logEntryId, count);
    }

    private void seedPrice(int modelId, int providerId, String typeName, String unit,
                           long minContextTokens, String serviceTier, long pricePerKUnit, String validFrom) {
        String tsz = (validFrom.endsWith("Z") || validFrom.contains("+"))
            ? validFrom : validFrom + "T00:00:00Z";
        jdbc.update("INSERT INTO token_types (name) VALUES (?) ON CONFLICT (name) DO NOTHING", typeName);
        Integer typeId = jdbc.queryForObject(
            "SELECT id FROM token_types WHERE name = ?", Integer.class, typeName);
        jdbc.update(
            "INSERT INTO token_prices (type_id, model_id, provider_id, unit, min_context_tokens, "
            + "service_tier, valid_from, price_per_k_unit) VALUES (?, ?, ?, ?, ?, ?, ?::timestamptz, ?)",
            typeId, modelId, providerId, unit, minContextTokens, serviceTier, tsz, pricePerKUnit);
    }

    private boolean columnExists(String table, String col) {
        Integer n = jdbc.queryForObject(
            "SELECT COUNT(*) FROM information_schema.columns "
            + "WHERE table_schema = 'public' AND table_name = ? AND column_name = ?",
            Integer.class, table, col);
        return n != null && n > 0;
    }

    private boolean indexExists(String table, String index) {
        Integer n = jdbc.queryForObject(
            "SELECT COUNT(*) FROM pg_indexes WHERE schemaname = 'public' "
            + "AND tablename = ? AND indexname = ?",
            Integer.class, table, index);
        return n != null && n > 0;
    }
}
