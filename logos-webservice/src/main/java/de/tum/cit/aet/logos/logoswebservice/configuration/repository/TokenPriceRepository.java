package de.tum.cit.aet.logos.logoswebservice.configuration.repository;

import java.util.List;
import java.util.Optional;

import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import de.tum.cit.aet.logos.logoswebservice.configuration.entity.TokenPrice;

public interface TokenPriceRepository extends JpaRepository<TokenPrice, Integer> {

    Optional<TokenPrice>
    findTopByModelIdAndTypeIdAndProviderIdAndUnitAndMinContextTokensAndServiceTierOrderByValidFromDesc(
            Integer modelId, Integer typeId, Integer providerId,
            String unit, Long minContextTokens, String serviceTier);

    List<TokenPrice> findByModelId(Integer modelId);

    @Query(value = """
        SELECT logos_resolve_unit_price(:quantity, :modelId, :providerId, now(), :contextTokens, 'default')
        """, nativeQuery = true)
    Long resolveCurrentDefaultUnitPrice(@Param("quantity") String quantity, @Param("modelId") Integer modelId,
                                       @Param("providerId") Integer providerId, @Param("contextTokens") long contextTokens);
}
