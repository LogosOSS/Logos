package de.tum.cit.aet.logos.logoswebservice.identity.service;

import java.io.IOException;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;
import de.tum.cit.aet.logos.logoswebservice.audit.AuditLogService;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.multipart.MultipartFile;

import de.tum.cit.aet.logos.logoswebservice.common.ConflictException;
import de.tum.cit.aet.logos.logoswebservice.identity.Csv;
import de.tum.cit.aet.logos.logoswebservice.identity.dto.AddTeamMemberRequestDTO;
import de.tum.cit.aet.logos.logoswebservice.identity.dto.CreateUserRequestDTO;
import de.tum.cit.aet.logos.logoswebservice.identity.dto.ImportUsersRequestDTO;
import de.tum.cit.aet.logos.logoswebservice.identity.dto.TeamResponseDTO;
import de.tum.cit.aet.logos.logoswebservice.identity.dto.UpdateUserInfoRequestDTO;
import de.tum.cit.aet.logos.logoswebservice.identity.dto.UserResponseDTO;
import de.tum.cit.aet.logos.logoswebservice.identity.entity.Role;
import de.tum.cit.aet.logos.logoswebservice.identity.entity.User;
import de.tum.cit.aet.logos.logoswebservice.identity.repository.TeamRepository;
import de.tum.cit.aet.logos.logoswebservice.identity.repository.UserRepository;

@Service
public class UserService {

    private final UserRepository userRepository;
    private final TeamRepository teamRepository;
    private final TeamService teamService;
    private final AuditLogService auditLog;
    private final TransactionTemplate transactions;

    public UserService(UserRepository userRepository, TeamRepository teamRepository, TeamService teamService,
                       AuditLogService auditLog,
                       TransactionTemplate transactions) {
        this.auditLog = auditLog;
        this.transactions = transactions;
        this.userRepository = userRepository;
        this.teamRepository = teamRepository;
        this.teamService = teamService;
    }

    public List<UserResponseDTO> listUsers() {
        return userRepository.findByIsActiveTrue().stream().map(this::toDto).toList();
    }

    public List<UserResponseDTO> listAdmins() {
        return userRepository.findAdmins().stream().map(this::toDto).toList();
    }

    public Map<String, Object> createUser(CreateUserRequestDTO body) {
        if (body.email() != null && !body.email().isBlank()
                && userRepository.existsByEmailIgnoreCase(body.email())) {
            throw new DuplicateEmailException();
        }

        String username = generateUsername(body.prename(), body.name());

        User user = new User();
        user.setUsername(username);
        user.setPrename(body.prename());
        user.setName(body.name());
        user.setEmail(body.email());
        user.setRole(body.role());
        User saved = userRepository.save(user);

        List<String> logosKeys = new ArrayList<>();
        if (body.team_ids() != null) {
            for (Integer teamId : body.team_ids()) {
                teamService.addMember(teamId, new AddTeamMemberRequestDTO(saved.getId(), false))
                    .ifPresent(logosKeys::add);
            }
        }

        List<TeamResponseDTO> teams = teamRepository.findTeamsForUser(saved.getId()).stream()
            .map(t -> new TeamResponseDTO(t.getId(), t.getName()))
            .toList();

        Map<String, Object> result = new LinkedHashMap<>();
        result.put("id", saved.getId());
        result.put("username", username);
        result.put("prename", saved.getPrename());
        result.put("name", saved.getName());
        result.put("email", saved.getEmail());
        result.put("role", saved.getRole());
        result.put("teams", teams);
        result.put("managed", saved.getKeycloakId() != null);
        result.put("logos_keys", logosKeys);
        return result;
    }

    public boolean deleteUser(Integer userId) {
        Optional<User> userOpt = userRepository.findById(userId);
        if (userOpt.isEmpty()) return false;
        requireUnmanaged(userOpt.get(), "deleted");
        userRepository.deleteById(userId);
        return true;
    }

    @Transactional
    public Optional<UserResponseDTO> updateRole(Integer userId, String role) {
        // Locked read: holds the user row until commit so a concurrent ownership
        // grant (which locks the same row before checking the role) cannot slip
        // in between the ownsAnyTeam check and the demotion.
        return userRepository.findByIdForUpdate(userId).map(user -> {
            String previous = user.getRole();
            if (Role.APP_DEVELOPER.matches(role) && teamService.ownsAnyTeam(userId)) {
                throw new ConflictException("User '" + user.getUsername()
                    + "' owns a team and team owners need the app_admin or logos_admin role."
                    + " Remove their ownership first.");
            }
            user.setRole(role);
            auditLog.record("user.role_changed", "user", userId, null, Map.of("role", previous), Map.of("role", role));
            return toDto(userRepository.save(user));
        });
    }

    public Optional<UserResponseDTO> updateInfo(Integer userId, UpdateUserInfoRequestDTO body) {
        return userRepository.findById(userId).map(user -> {
            requireUnmanaged(user, "edited");
            if (body.prename() != null) user.setPrename(body.prename());
            if (body.name() != null) user.setName(body.name());
            if (body.email() != null) user.setEmail(body.email());
            return toDto(userRepository.save(user));
        });
    }

    /**
     * Parses the uploaded CSV and returns its header row plus the raw data rows
     * so the web application can map columns, let the user pick rows and show a
     * preview before anything is written.
     */
    public Map<String, Object> previewImport(MultipartFile file) throws IOException {
        List<String[]> records = Csv.parse(new String(file.getBytes()));
        List<String> columns = new ArrayList<>();
        List<List<String>> rows = new ArrayList<>();
        for (int i = 0; i < records.size(); i++) {
            if (i == 0) {
                columns = Arrays.asList(records.get(0));
            } else {
                rows.add(Arrays.asList(records.get(i)));
            }
        }
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("columns", columns);
        result.put("rows", rows);
        return result;
    }

    /**
     * Creates the users the web application mapped and selected from the preview.
     * Each row commits together with its optional team membership.
     */
    public Map<String, Object> importUsers(List<ImportUsersRequestDTO.Row> rowsIn, Integer targetTeamId, Integer callerId, boolean platformAdmin) {
        List<Map<String, Object>> rows = new ArrayList<>();
        int created = 0, existing = 0, failed = 0;
        for (ImportUsersRequestDTO.Row in : rowsIn) {
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("email", trimToNull(in.email()));
            row.put("username", null);
            row.put("error", null);
            try {
                transactions.executeWithoutResult(tx -> importRow(row, in, targetTeamId, callerId, platformAdmin));
                if ("existing".equals(row.get("status"))) existing++;
                else created++;
            } catch (RuntimeException e) {
                row.put("username", null);
                row.remove("membership");
                row.put("error", e instanceof IllegalArgumentException ? e.getMessage() : "Could not import this row");
                row.put("status", "failed");
                failed++;
            }
            rows.add(row);
        }
        Map<String, Object> summary = new LinkedHashMap<>();
        summary.put("created",  created);
        summary.put("existing", existing);
        summary.put("failed",   failed);
        return Map.of("summary", summary, "rows", rows);
    }

    private void importRow(Map<String, Object> row, ImportUsersRequestDTO.Row in,
                           Integer targetTeamId, Integer callerId, boolean platformAdmin) {
        String email = trimToNull(in.email());
        // Lock identity before team, in the same order as account reactivation.
        Optional<User> match = email == null ? Optional.empty() : userRepository.findByEmailForUpdate(email);
        Integer teamId = targetTeamId;
        if (in.team() != null) {
            if (teamId != null) throw new IllegalArgumentException("Select one team source for this row");
            teamId = teamRepository.findByName(in.team().strip())
                .orElseThrow(() -> new IllegalArgumentException("Unknown team name")).getId();
        }
        if (teamId != null) {
            var team = teamRepository.findByIdForUpdate(teamId)
                .orElseThrow(() -> new IllegalArgumentException("Unknown team"));
            if (!platformAdmin && !teamService.isOwner(teamId, callerId)) {
                throw new IllegalArgumentException("Team owner access required");
            }
            if (email == null) throw new IllegalArgumentException("Email is required for membership");
            row.put("team", team.getName());
        }
        User user = match.orElseGet(() -> {
            String prename = trimToNull(in.prename());
            String name = trimToNull(in.name());
            User fresh = new User();
            fresh.setUsername(generateUsername(prename, name));
            fresh.setPrename(prename);
            fresh.setName(name);
            fresh.setEmail(email);
            fresh.setRole(Role.Names.APP_DEVELOPER);
            return userRepository.save(fresh);
        });
        if (teamId != null) {
            if (!user.isActive()) throw new IllegalArgumentException("Account is inactive");
            boolean alreadyMember = teamService.isMember(teamId, user.getId());
            if (!alreadyMember) {
                teamService.addMember(teamId, new AddTeamMemberRequestDTO(user.getId(), false))
                    .orElseThrow(() -> new IllegalArgumentException("Could not assign team membership"));
            }
            row.put("membership", alreadyMember ? "Already a member" : "Member added");
        }
        row.put("username", user.getUsername());
        row.put("status", match.isPresent() ? "existing" : "created");
    }

    /**
     * Keycloak owns the identity and existence of synced users: every sync
     * overwrites prename/name/email and reactivates the account. Editing those
     * locally would just be undone on the next sync, so we reject it outright.
     * Logos-owned data (team limits, key budgets, team ownership) stays editable.
     */
    private void requireUnmanaged(User user, String action) {
        if (user.getKeycloakId() != null) {
            throw new ConflictException("This user is managed by Keycloak and cannot be " + action + " here.");
        }
    }

    private static String trimToNull(String value) {
        if (value == null) return null;
        String trimmed = value.trim();
        return trimmed.isEmpty() ? null : trimmed;
    }

    public UserResponseDTO toDto(User u) {
        List<TeamResponseDTO> teams = teamRepository.findTeamsForUser(u.getId()).stream()
            .map(t -> new TeamResponseDTO(t.getId(), t.getName()))
            .toList();
        return new UserResponseDTO(u.getId(), u.getUsername(), u.getPrename(), u.getName(), u.getRole(), u.getEmail(), teams, u.getKeycloakId() != null);
    }

    private String generateUsername(String prename, String name) {
        String p = prename == null ? "" : prename.strip().toLowerCase().replaceAll("\\s+", "");
        String n = name    == null ? "" : name.strip().toLowerCase().replaceAll("\\s+", "");

        List<String> candidates = new ArrayList<>();
        for (int i = 1; i <= p.length(); i++) {
            candidates.add(p.substring(0, i) + n);
        }
        if (candidates.isEmpty()) candidates.add(n.isBlank() ? "user" : n);

        for (String candidate : candidates) {
            if (!userRepository.existsByUsername(candidate)) return candidate;
        }

        String base = p.isBlank() ? n : (p + n);
        if (base.isBlank()) base = "user";
        for (int i = 2; ; i++) {
            String candidate = base + i;
            if (!userRepository.existsByUsername(candidate)) return candidate;
        }
    }

    public static class DuplicateEmailException extends RuntimeException {
        public DuplicateEmailException() { super("Email already in use"); }
    }
}
