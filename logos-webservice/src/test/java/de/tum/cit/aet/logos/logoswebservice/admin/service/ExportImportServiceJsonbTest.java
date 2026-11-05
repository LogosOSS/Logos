package de.tum.cit.aet.logos.logoswebservice.admin.service;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertSame;

import java.sql.SQLException;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import org.junit.jupiter.api.Test;
import org.postgresql.util.PGobject;
import org.springframework.jdbc.core.JdbcTemplate;

import com.fasterxml.jackson.databind.ObjectMapper;

class ExportImportServiceJsonbTest {

    private final ExportImportService service =
        new ExportImportService(new JdbcTemplate(), new ObjectMapper());

    private static PGobject pg(String type, String value) throws SQLException {
        PGobject object = new PGobject();
        object.setType(type);
        object.setValue(value);
        return object;
    }

    private Object decode(Object value) {
        Map<String, Object> row = new LinkedHashMap<>();
        row.put("col", value);
        return service.decodeJsonbColumns(List.of(row)).get(0).get("col");
    }

    @Test
    void jsonbValuesAreDecodedToTheirDocument() throws SQLException {
        assertEquals(List.of(1, 2), decode(pg("jsonb", "[1,2]")));
        assertEquals(Map.of("a", "b"), decode(pg("json", "{\"a\":\"b\"}")));
    }

    @Test
    void blankJsonbIsNull() throws SQLException {
        assertNull(decode(pg("jsonb", " ")));
    }

    @Test
    void otherPgTypesAndPlainValuesPassThrough() throws SQLException {
        PGobject interval = pg("interval", "1 day");
        assertSame(interval, decode(interval));
        assertEquals("text", decode("text"));
        assertNull(decode(null));
    }

    @Test
    void anAbsentDriverClassIsNoMatch() {
        assertNull(ExportImportService.loadOptionalClass("org.example.DoesNotExist"));
        assertEquals(PGobject.class, ExportImportService.loadOptionalClass("org.postgresql.util.PGobject"));
    }
}
