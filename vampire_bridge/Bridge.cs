// Original integration bridge. Game assemblies are runtime references only.
using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace JevVampire
{
    public sealed class Bridge : MonoBehaviour
    {
        static Bridge instance;
        static readonly BindingFlags Flags = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static;
        static readonly Dictionary<string, Type> Types = new Dictionary<string, Type>();
        TcpListener listener;
        Socket client;
        readonly StringBuilder incoming = new StringBuilder();
        readonly Decoder decoder = Encoding.UTF8.GetDecoder();
        readonly Queue<byte[]> outgoing = new Queue<byte[]>();
        int sendOffset;
        long sequence;
        long observedAt;
        long consumedSequence = -1;
        string observedFingerprint;
        string observedPhase;
        string observedScene;
        int observedPlayer;
        Vector2 direction;
        long leaseUntil;
        long manualUntil;
        object controlledPlayer;
        object manager;
        object machine;
        long lastDiscovery;
        string phase = "menu";
        string state = "";
        string scene = "";
        string lastError;
        bool recording;
        string recordPath;
        string recordError;
        StreamWriter frameIndex;
        long recordStarted;
        long recordEnded;
        int frameCount;
        int lastCaptureWidth;
        int lastCaptureHeight;
        float recordFps = 4;
        long nextFrame;
        bool capturePending;
        bool muted;
        bool quitAfterFlush;

        sealed class Option
        {
            public string Id;
            public string Label;
            public string Description;
            public string Kind;
            public UnityEngine.Object Target;
            public Action Invoke;
            public JObject Json() { return new JObject { ["id"] = Id, ["label"] = Label, ["description"] = Description, ["kind"] = Kind }; }
        }

        public static void Bootstrap()
        {
            if (instance != null) return;
            try
            {
                var go = new GameObject("JevVampireBridge");
                DontDestroyOnLoad(go);
                instance = go.AddComponent<Bridge>();
                instance.listener = new TcpListener(IPAddress.Loopback, 4244);
                instance.listener.Start(1);
                Application.runInBackground = true;
                instance.muted = Environment.GetEnvironmentVariable("JEV_MUTE") == "1";
                if (instance.muted) AudioListener.volume = 0;
                Debug.Log("Jev Vampire bridge listening on 127.0.0.1:4244");
            }
            catch (Exception ex) { Debug.LogError("Jev bridge initialization failed: " + ex.GetType().Name + ": " + ex.Message); }
        }

        // Called after the native input handler. Real keyboard/controller input wins.
        public static void AfterInput(object character)
        {
            var self = instance;
            if (self == null || self.client == null || self.phase != "combat" || !ReferenceEquals(character, self.controlledPlayer)) return;
            try
            {
                if (Bool(Read(character, "IsDead")) || (Number(TryCall(character, "MaxHp")) > 0 && Number(Read(character, "CurrentHp")) <= 0))
                {
                    self.leaseUntil = 0;
                    return;
                }
                var raw = Read(character, "CurrentDirectionRaw");
                if (raw is Vector2 && ((Vector2)raw).sqrMagnitude > 0.0001f)
                {
                    self.manualUntil = Now() + 1000;
                    self.leaseUntil = 0;
                    return;
                }
                if (Now() >= self.leaseUntil || Now() < self.manualUntil || Bool(Read(character, "_blockInput"))) return;
                Write(character, "CurrentDirectionRaw", self.direction);
                Call(character, "ProcessRawDirection");
            }
            catch (Exception ex) { self.leaseUntil = 0; self.lastError = "input: " + ex.GetType().Name; }
        }

        void Update()
        {
            try
            {
                if (muted) AudioListener.volume = 0;
                RefreshState();
                if (phase != "combat") leaseUntil = 0;
                if (listener != null && listener.Pending())
                {
                    var accepted = listener.AcceptSocket();
                    if (client != null) accepted.Close();
                    else { client = accepted; client.Blocking = false; client.NoDelay = true; }
                }
                Pump();
                if (quitAfterFlush && outgoing.Count == 0) { StopRecording(); Application.Quit(); return; }
                if (recording && !capturePending && Now() >= nextFrame)
                {
                    capturePending = true;
                    nextFrame = Now() + (long)(1000 / recordFps);
                    StartCoroutine(Capture());
                }
            }
            catch (Exception ex) { lastError = ex.GetType().Name + ": " + ex.Message; Disconnect(); }
        }

        void Pump()
        {
            if (client == null) return;
            if (client.Poll(0, SelectMode.SelectRead) && client.Available == 0) { Disconnect(); return; }
            var buffer = new byte[8192];
            int budget = 65536;
            while (client != null && client.Available > 0 && budget > 0)
            {
                int n = client.Receive(buffer, 0, Math.Min(buffer.Length, budget), SocketFlags.None);
                if (n == 0) { Disconnect(); return; }
                char[] decoded = new char[n + 4];
                int chars = decoder.GetChars(buffer, 0, n, decoded, 0, false);
                incoming.Append(decoded, 0, chars);
                budget -= n;
                if (incoming.Length > 131072) { Disconnect(); return; }
            }
            for (int i = 0; i < 16; i++)
            {
                var all = incoming.ToString();
                int end = all.IndexOf('\n');
                if (end < 0) break;
                string line = all.Substring(0, end).Trim();
                incoming.Remove(0, end + 1);
                if (line.Length > 0) Handle(line);
            }
            int sendBudget = 262144;
            while (client != null && outgoing.Count > 0 && sendBudget > 0 && client.Poll(0, SelectMode.SelectWrite))
            {
                byte[] data = outgoing.Peek();
                int n;
                try { n = client.Send(data, sendOffset, Math.Min(data.Length - sendOffset, sendBudget), SocketFlags.None); }
                catch (SocketException ex) { if (ex.SocketErrorCode == SocketError.WouldBlock) break; throw; }
                sendOffset += n;
                sendBudget -= n;
                if (sendOffset == data.Length) { outgoing.Dequeue(); sendOffset = 0; }
                if (n == 0) break;
            }
            if (outgoing.Count > 32) Disconnect();
        }

        void Handle(string line)
        {
            JToken id = JValue.CreateNull();
            try
            {
                var request = JObject.Parse(line);
                id = request["id"] ?? JValue.CreateNull();
                string command = (string)request["cmd"];
                JObject result;
                switch (command)
                {
                    case "observe": result = Observe(); break;
                    case "move": result = Move(request); break;
                    case "release": leaseUntil = 0; result = new JObject { ["released"] = true }; break;
                    case "pause": result = Pause(); break;
                    case "quit": leaseUntil = 0; quitAfterFlush = true; result = new JObject { ["quitting"] = true }; break;
                    case "act": result = Act(request); break;
                    case "record_start": result = StartRecording(request); break;
                    case "record_stop": StopRecording(); result = RecordingStatus(); break;
                    case "record_status": result = RecordingStatus(); break;
                    default: throw new InvalidOperationException("unknown_command");
                }
                Reply(new JObject { ["id"] = id, ["ok"] = true, ["result"] = result });
            }
            catch (Exception ex)
            {
                if (ex is TargetInvocationException && ex.InnerException != null) ex = ex.InnerException;
                Reply(new JObject { ["id"] = id, ["ok"] = false, ["error"] = ex.Message });
            }
        }

        void Reply(JObject value) { outgoing.Enqueue(Encoding.UTF8.GetBytes(value.ToString(Formatting.None) + "\n")); }

        void Disconnect()
        {
            leaseUntil = 0;
            controlledPlayer = null;
            consumedSequence = sequence;
            if (client != null) { try { client.Close(); } catch { } client = null; }
            incoming.Clear(); decoder.Reset(); outgoing.Clear(); sendOffset = 0;
        }

        void RefreshState()
        {
            scene = SceneManager.GetActiveScene().name;
            if (Now() - lastDiscovery > 250 || !Alive(manager) || !Alive(machine))
            {
                manager = Find("VampireSurvivors.Framework.GameManager");
                machine = Find("VampireSurvivors.GameStateMachine");
                lastDiscovery = Now();
            }
            state = Convert.ToString(Read(machine, "CurrentStateName")) ?? "";
            controlledPlayer = Read(manager, "Player");
            bool online = Bool(Read(manager, "IsOnlineMultiplayer"));
            if (online) { phase = "menu"; leaseUntil = 0; return; }
            if (state.EndsWith("GameStatePlaying", StringComparison.Ordinal) && Alive(controlledPlayer))
                phase = Bool(Read(controlledPlayer, "IsDead")) ||
                        (Number(TryCall(controlledPlayer, "MaxHp")) > 0 && Number(Read(controlledPlayer, "CurrentHp")) <= 0)
                        ? "gameover" : "combat";
            else if (state.EndsWith("GameStateLevelUp", StringComparison.Ordinal)) phase = "levelup";
            else if (state.EndsWith("GameStateOpenTreasure", StringComparison.Ordinal) || state.EndsWith("GameStateTreasure", StringComparison.Ordinal)) phase = "chest";
            else if (state.IndexOf("GameOver", StringComparison.OrdinalIgnoreCase) >= 0 || state.IndexOf("Recap", StringComparison.OrdinalIgnoreCase) >= 0 || state.EndsWith("GameStatePlayerDied", StringComparison.Ordinal)) phase = "gameover";
            else phase = "menu";
        }

        JObject Observe()
        {
            RefreshState();
            var options = Options();
            sequence++;
            observedAt = Now();
            observedPhase = phase;
            observedScene = scene;
            observedPlayer = ObjectId(controlledPlayer);
            observedFingerprint = Fingerprint(options);
            var pos = Position(controlledPlayer);
            var facing = Vector(Read(controlledPlayer, "LastMovementDirection"));
            var player = new JObject
            {
                ["x"] = pos.x, ["y"] = pos.y,
                ["facing_x"] = facing.x, ["facing_y"] = facing.y,
                ["hp"] = Number(Read(controlledPlayer, "CurrentHp")),
                ["max_hp"] = Number(TryCall(controlledPlayer, "MaxHp")),
                ["level"] = Number(Read(controlledPlayer, "Level")),
                ["speed"] = Number(TryCall(controlledPlayer, "PMoveSpeed")) * Number(Read(GameType("VampireSurvivors.Framework.GameManager"), "PlayerPxSpeed")),
                ["radius"] = Radius(controlledPlayer),
                ["pickup_radius"] = Radius(Read(controlledPlayer, "Magnet")),
                ["xp"] = Number(Read(controlledPlayer, "Xp")),
                ["character"] = Convert.ToString(Read(controlledPlayer, "CharacterType")) ?? ""
            };
            var stats = new JObject();
            foreach (var stat in new Dictionary<string, string> { { "armor", "PArmor" }, { "cooldown", "PCooldown" }, { "amount", "PAmount" }, { "growth", "PGrowth" } })
            {
                var value = TryCall(controlledPlayer, stat.Value);
                if (value != null) stats[stat.Key] = Number(value);
            }
            if (stats.Count > 0) player["stats"] = stats;
            var observedEnemies = Items(Read(Read(manager, "Stage"), "SpawnedEnemies")).Where(Alive).ToList();
            var enemies = new JArray();
            foreach (var enemy in observedEnemies.OrderBy(e => (Position(e) - pos).sqrMagnitude).Take(256))
            {
                var p = Position(enemy);
                var v = Vector(Read(enemy, "Velocity"));
                var observedEnemy = new JObject { ["id"] = ObjectId(enemy), ["type"] = Convert.ToString(Read(enemy, "EnemyType")) ?? enemy.GetType().Name, ["x"] = p.x, ["y"] = p.y, ["radius"] = Radius(enemy), ["velocity_x"] = v.x, ["velocity_y"] = v.y };
                AddObservedNumber(observedEnemy, "hp", Read(enemy, "Hp"));
                enemies.Add(observedEnemy);
            }
            var pickups = new JArray();
            var seen = new HashSet<int>();
            foreach (var item in Items(Read(manager, "Gems")).Concat(Items(Read(manager, "StagePickups"))).Where(Alive).OrderBy(e => (Position(e) - pos).sqrMagnitude).Take(64))
            {
                if (!seen.Add(ObjectId(item))) continue;
                var p = Position(item);
                string type = item.GetType().Name;
                string dataType = Convert.ToString(Read(item, "ItemType")) ?? "";
                string text = (type + " " + dataType).ToLowerInvariant();
                string kind = text.Contains("gem") ? "experience" : text.Contains("treasure") ? "chest" : text.Contains("roast") || text.Contains("chicken") ? "healing" : "pickup";
                pickups.Add(new JObject { ["id"] = ObjectId(item), ["x"] = p.x, ["y"] = p.y, ["kind"] = kind, ["value"] = Number(Read(item, "Value")), ["moving_to_player"] = Bool(Read(item, "GoToPlayer")), ["name"] = dataType.Length > 0 ? dataType : type });
            }
            var inventory = new JArray();
            var weapons = Read(controlledPlayer, "WeaponsManager") ?? Read(controlledPlayer, "weaponsManager");
            var accessories = Read(controlledPlayer, "AccessoriesManager");
            var weaponType = GameType("VampireSurvivors.Objects.Weapons.Weapon");
            foreach (var w in Items(Read(weapons, "ActiveEquipment")).Concat(Items(Read(accessories, "ActiveEquipment"))).Take(24))
            {
                var equipment = new JObject { ["name"] = Convert.ToString(Read(w, "WeaponType") ?? Read(w, "Type") ?? Read(w, "_type")) ?? w.GetType().Name, ["level"] = Number(Read(w, "Level") ?? Read(w, "_level")) };
                if (weaponType != null && weaponType.IsInstanceOfType(w)) equipment["attack"] = ObserveWeapon(w);
                inventory.Add(equipment);
            }
            return new JObject
            {
                ["protocol"] = 1, ["game"] = "vampire_survivors", ["seq"] = sequence,
                ["phase"] = phase, ["state"] = state, ["scene"] = scene,
                ["elapsed"] = Number(Read(manager, "SurvivedSeconds")), ["time_ms"] = observedAt,
                ["player"] = player, ["enemies"] = enemies, ["pickups"] = pickups,
                ["enemy_count_total"] = observedEnemies.Count, ["enemy_count_exported"] = enemies.Count,
                ["enemy_snapshot_truncated"] = observedEnemies.Count > enemies.Count,
                ["menu"] = new JObject { ["options"] = new JArray(options.Select(o => o.Json())) },
                ["inventory"] = inventory, ["recording"] = RecordingStatus(),
                ["manual_override"] = Now() < manualUntil, ["diagnostic"] = lastError ?? "",
                ["ui_diagnostic"] = UiDiagnostic()
            };
        }

        static JObject ObserveWeapon(object weapon)
        {
            var attack = new JObject();
            // Native power includes player modifiers; critical hits and target modifiers
            // can change damage dealt. PInterval is in milliseconds in this game build.
            AddObservedNumber(attack, "power", TryCall(weapon, "PPower"));
            AddObservedNumber(attack, "amount", TryCall(weapon, "PAmount"));
            AddObservedNumber(attack, "area_multiplier", TryCall(weapon, "PArea"));
            AddObservedNumber(attack, "interval_seconds", TryCall(weapon, "PInterval"), 0.001);
            var timer = Read(weapon, "_firingTimer");
            var done = Read(timer, "IsDone");
            var paused = Read(timer, "IsPaused");
            if (done is bool && !(bool)done && paused is bool)
            {
                if ((bool)paused) attack["cycle_paused"] = true;
                // This is the repeating firing cycle, not the next staggered projectile.
                else AddObservedNumber(attack, "cycle_remaining_seconds", TryCall(timer, "GetTimeRemaining"));
            }
            bool homing = Bool(Read(weapon, "IsHoming"));
            switch (weapon.GetType().Name)
            {
                case "KnifeWeapon": attack["aim_mode"] = homing ? "nearest_enemy_on_fire" : "last_movement_direction"; break;
                case "AxeWeapon": attack["aim_mode"] = homing ? "nearest_enemy_then_arc" : "upward_arc_with_facing_spread"; break;
                case "MagicMissileWeapon": attack["aim_mode"] = "nearest_enemy_on_fire"; break;
                case "WhipWeapon": attack["aim_mode"] = "horizontal_alternating_from_facing"; break;
            }
            return attack;
        }

        static void AddObservedNumber(JObject result, string name, object value, double scale = 1)
        {
            if (value == null) return;
            try
            {
                double number = Convert.ToDouble(value, CultureInfo.InvariantCulture) * scale;
                if (!double.IsNaN(number) && !double.IsInfinity(number)) result[name] = number;
            }
            catch { }
        }

        JObject Move(JObject request)
        {
            RefreshState();
            ValidateSequence(request, 1000);
            if (phase != "combat" || observedPhase != phase || observedScene != scene || observedPlayer != ObjectId(controlledPlayer)) throw new InvalidOperationException("not_in_combat");
            if (Now() < manualUntil) throw new InvalidOperationException("manual_override");
            double x = RequiredNumber(request, "dx"), y = RequiredNumber(request, "dy");
            double ttl = RequiredNumber(request, "ttl_ms");
            if (Math.Abs(x) > 1 || Math.Abs(y) > 1 || ttl < 1 || ttl > 1000) throw new InvalidOperationException("invalid_move");
            direction = Vector2.ClampMagnitude(new Vector2((float)x, (float)y), 1f);
            leaseUntil = Now() + (long)ttl;
            consumedSequence = sequence;
            return new JObject { ["applied"] = true, ["dx"] = direction.x, ["dy"] = direction.y, ["ttl_ms"] = ttl };
        }

        JObject Act(JObject request)
        {
            RefreshState();
            ValidateSequence(request, 30000);
            if (phase == "combat" || observedPhase != phase || scene != observedScene || observedPlayer != ObjectId(controlledPlayer)) throw new InvalidOperationException("phase_changed");
            var options = Options();
            if (Fingerprint(options) != observedFingerprint) throw new InvalidOperationException("menu_changed");
            string action = (string)request["action_id"];
            var option = options.FirstOrDefault(o => o.Id == action);
            if (option == null) throw new InvalidOperationException("illegal_action");
            consumedSequence = sequence;
            leaseUntil = 0;
            option.Invoke();
            return new JObject { ["applied"] = true, ["action_id"] = action };
        }

        JObject Pause()
        {
            leaseUntil = 0;
            RefreshState();
            if (phase != "combat") return new JObject { ["paused"] = state.EndsWith("GameStatePaused", StringComparison.Ordinal), ["phase"] = phase };
            // Use the state transition used by the pause UI, including its normal enter callbacks.
            machine.GetType().GetMethod("FireEvent", Flags, null, new[] { typeof(string) }, null).Invoke(machine, new object[] { "PAUSE_GAME" });
            RefreshState();
            return new JObject { ["paused"] = true };
        }

        void ValidateSequence(JObject request, long ageLimit)
        {
            if (request["seq"] == null || request["seq"].Type != JTokenType.Integer || (long)request["seq"] != sequence || sequence < 1 || consumedSequence == sequence) throw new InvalidOperationException("stale_sequence");
            if (Now() - observedAt > ageLimit) throw new InvalidOperationException("observation_expired");
        }

        List<Option> Options()
        {
            var result = new List<Option>();
            if (Bool(Read(manager, "IsOnlineMultiplayer"))) return result;
            if (phase == "levelup")
            {
                var page = Find("VampireSurvivors.UI.LevelUpPage");
                if (!PageVisible(page) || Bool(Read(page, "_hasSelected"))) return result;
                bool banish = Bool(Read(page, "_isBanishMode"));
                foreach (var item in Items(Read(page, "LevelUpItems")).Where(Alive))
                {
                    if (!Interactable(item)) continue;
                    var captured = item;
                    string weapon = Convert.ToString(TryCall(item, "GetWeaponType")) ?? "";
                    string description = TextOf(item);
                    result.Add(new Option { Id = (banish ? "banish:" : "select:") + Convert.ToString(Read(item, "Index")), Label = (banish ? "Banish " : "Choose ") + (weapon.Length > 0 && weapon != "VOID" ? weapon : Convert.ToString(Read(item, "ItemType"))), Description = description, Kind = banish ? "banish" : "upgrade", Target = item as UnityEngine.Object, Invoke = () => Call(captured, "Select") });
                }
                if (banish) Add(result, page, "cancel_banish", "Cancel banish", "CancelBanishMode", Read(page, "CancelButton"));
                else
                {
                    if (Bool(Read(page, "HasReRolls"))) Add(result, page, "reroll", "Reroll choices", "Reroll", Read(page, "RerollButton"));
                    if (Bool(Read(page, "HasSkips"))) Add(result, page, "skip", "Skip this level-up", "Skip", Read(page, "SkipButton"));
                    if (Bool(Read(page, "HasBanish"))) Add(result, page, "banish_mode", "Choose an item to banish", "SetBanishMode", Read(page, "BanishButton"));
                }
            }
            else if (phase == "chest")
            {
                var page = Find("VampireSurvivors.UI.OpenTreasurePage");
                if (!PageVisible(page)) return result;
                if (!Bool(Read(page, "_openButtonPressed"))) Add(result, page, "open_chest", "Open treasure chest", "OpenTreasure", Read(page, "OpenButton"));
                if (Bool(Read(page, "_animationFinished")) && !Bool(Read(page, "_doneButtonPressed"))) Add(result, page, "claim_chest", "Claim treasure and continue", "ClaimTreasure", Read(page, "DoneButton"));
            }
            else if (phase == "menu" && state.EndsWith("GameStatePaused", StringComparison.Ordinal))
            {
                var pausedState = Read(machine, "currentState");
                if (Alive(pausedState) && pausedState.GetType().Name == "GameStatePaused")
                    result.Add(new Option { Id = "resume_run", Label = "Resume the paused run", Description = "Continue using the normal pause-menu resume callback.", Kind = "setup", Target = pausedState as UnityEngine.Object, Invoke = () => Call(pausedState, "ReturnToGame") });
            }
            else if (phase == "menu" && !Alive(manager))
            {
                // A small allowlist for initial solo setup; purchases and option changes are excluded.
                var stage = Find("VampireSurvivors.UI.StageSelectPage");
                var character = Find("VampireSurvivors.UI.CharacterSelectionPage");
                var main = Find("VampireSurvivors.UI.MainMenuPage");
                var warning = Find("VampireSurvivors.UI.WarningPage");
                var landing = Find("VampireSurvivors.UI.LandingScreenPage");
                if (PageVisible(warning))
                {
                    if (Bool(Read(warning, "_isWaiting")) && Number(Read(warning, "_currentTime")) > Number(Read(warning, "WaitDuration")))
                        result.Add(new Option { Id = "dismiss_warning", Label = "Continue past the photosensitivity notice", Description = "Activate the game's Press to Start action after its built-in waiting period.", Kind = "setup", Target = warning as UnityEngine.Object, Invoke = () => Call(warning, "Complete") });
                }
                else if (Alive(landing))
                    result.Add(new Option { Id = "press_start", Label = "Continue from the title screen", Description = "Activate the title screen's native press-to-start callback.", Kind = "setup", Target = landing as UnityEngine.Object, Invoke = () => Call(landing, "MoveToNextView") });
                else if (PageVisible(stage))
                {
                    var selected = Read(stage, "_selectedStage");
                    var data = Read(stage, "_selectedData");
                    if (selected != null && Bool(Read(data, "unlocked")))
                    {
                        string name = Convert.ToString(Read(selected, "Type")) ?? "current stage";
                        Add(result, stage, "select_stage", "Select " + name, "SelectStage", Read(stage, "_SelectButton"));
                        if (Bool(Read(stage, "_hasConfirmed"))) Add(result, stage, "start_run", "Start run on " + name, "ConfirmStage", Read(stage, "_ConfirmButton"));
                    }
                }
                else if (PageVisible(character))
                {
                    var multiplayer = Read(character, "_multiplayer");
                    var selected = Read(character, "_selectedCharacterItemUI");
                    if (!Bool(Read(multiplayer, "IsMultiplayer")) && !Bool(Read(character, "PartyModeEnabled")) && Bool(TryCall(selected, "IsAvailable")))
                    {
                        string name = Convert.ToString(Read(character, "_currentType")) ?? "current character";
                        var button = Read(character, "ConfirmButton");
                        if (!Bool(Read(character, "_characterConfirmed")) && Alive(button) && Interactable(button))
                            result.Add(new Option { Id = "select_character", Label = "Select " + name, Description = "Select the currently highlighted, already available character.", Kind = "setup", Target = character as UnityEngine.Object, Invoke = () => character.GetType().GetMethod("SelectCharacter", Flags, null, new[] { typeof(bool) }, null).Invoke(character, new object[] { false }) });
                        if (Bool(Read(character, "_characterConfirmed"))) Add(result, character, "confirm_character", "Continue with " + name, "ConfirmCharacter", Read(character, "StartButton"));
                    }
                }
                else if (PageVisible(main)) Add(result, main, "start_solo", "Start a solo run", "ShowCharacterSelect", Read(main, "_StartButton"));
            }
            // Unknown menus remain manual until their exact native callbacks are verified.
            return result;
        }

        static void Add(List<Option> list, object page, string id, string label, string method, object button)
        {
            if (!Alive(page) || !Alive(button) || !Interactable(button)) return;
            list.Add(new Option { Id = id, Label = label, Description = TextOf(button), Kind = id, Target = page as UnityEngine.Object, Invoke = () => Call(page, method) });
        }

        static bool PageVisible(object page)
        {
            // Doozy can hide an enabled page by disabling its Canvas, leaving the GameObject active.
            return Alive(page) && Bool(Read(Read(page, "View"), "IsVisible"));
        }

        JObject UiDiagnostic()
        {
            var result = new JObject { ["manager"] = ObjectSummary(manager), ["scene"] = scene };
            string[] names = { "MainMenuPage", "CharacterSelectionPage", "StageSelectPage", "WarningPage", "LandingScreenPage" };
            string[][] fields = { new[] { "_StartButton" }, new[] { "ConfirmButton", "StartButton", "BuyButton" }, new[] { "_SelectButton", "_ConfirmButton" }, new string[0], new string[0] };
            for (int i = 0; i < names.Length; i++)
            {
                var page = Find("VampireSurvivors.UI." + names[i]);
                var data = ObjectSummary(page);
                var view = Read(page, "View");
                data["view_visibility"] = Convert.ToString(Read(view, "Visibility")) ?? "";
                data["page_visible"] = PageVisible(page);
                data["canvas_enabled"] = Bool(Read(Read(view, "Canvas"), "enabled"));
                var canvasGroup = Read(view, "CanvasGroup");
                data["canvas_alpha"] = Number(Read(canvasGroup, "alpha"));
                data["canvas_interactable"] = Bool(Read(canvasGroup, "interactable"));
                var buttons = new JObject();
                foreach (string field in fields[i])
                {
                    var button = Read(page, field);
                    var buttonData = ObjectSummary(button);
                    buttonData["interactable"] = Interactable(button);
                    buttons[field] = buttonData;
                }
                data["buttons"] = buttons;
                result[names[i]] = data;
            }
            return result;
        }

        static JObject ObjectSummary(object value)
        {
            var component = value as Component;
            var go = value as GameObject;
            if (component != null) go = component.gameObject;
            string path = "";
            if (go != null) for (var t = go.transform; t != null; t = t.parent) path = t.name + (path.Length == 0 ? "" : "/" + path);
            return new JObject { ["found"] = value != null, ["alive"] = Alive(value), ["active"] = go != null && go.activeInHierarchy, ["enabled"] = !(value is Behaviour) || ((Behaviour)value).enabled, ["path"] = path };
        }

        string Fingerprint(List<Option> options)
        {
            return phase + "|" + state + "|" + scene + "|" + ObjectId(controlledPlayer) + "|" + string.Join(";", options.Select(o => o.Id + ":" + ObjectId(o.Target) + ":" + o.Label + ":" + o.Description));
        }

        JObject StartRecording(JObject request)
        {
            if (recording) throw new InvalidOperationException("already_recording");
            ValidateCaptureDimensions(Screen.width, Screen.height);
            string root = Environment.GetEnvironmentVariable("JEV_RECORD_ROOT");
            string desired = (string)request["path"];
            if (string.IsNullOrEmpty(root) || string.IsNullOrEmpty(desired)) throw new InvalidOperationException("record_root_not_configured");
            root = Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar);
            desired = Path.GetFullPath(desired).TrimEnd(Path.DirectorySeparatorChar);
            if (Path.GetDirectoryName(desired) != root || !Path.GetFileName(desired).StartsWith("vampire-", StringComparison.Ordinal)) throw new InvalidOperationException("record_path_outside_root");
            if (Directory.Exists(desired) && Directory.EnumerateFileSystemEntries(desired).Any(p => Path.GetFileName(p) != "metadata.json" && Path.GetFileName(p) != "decisions.jsonl")) throw new InvalidOperationException("record_directory_not_empty");
            if (Directory.Exists(desired) && (File.GetAttributes(desired) & FileAttributes.ReparsePoint) != 0) throw new InvalidOperationException("record_symlink_rejected");
            recordFps = (float)Math.Max(1, Math.Min(4, request["fps"] == null ? 4 : RequiredNumber(request, "fps")));
            Directory.CreateDirectory(desired);
            Directory.CreateDirectory(Path.Combine(desired, "frames"));
            recordPath = desired;
            frameIndex = new StreamWriter(Path.Combine(desired, "frames.jsonl"), false, new UTF8Encoding(false));
            frameIndex.AutoFlush = true;
            frameCount = 0; lastCaptureWidth = 0; lastCaptureHeight = 0; recordError = null; recordStarted = Now(); recordEnded = 0; nextFrame = recordStarted; recording = true;
            File.WriteAllText(Path.Combine(desired, "capture.json"), new JObject { ["game"] = "vampire_survivors", ["fps"] = recordFps, ["started_at_ms"] = recordStarted, ["width"] = Screen.width, ["height"] = Screen.height }.ToString(Formatting.Indented));
            return RecordingStatus();
        }

        IEnumerator Capture()
        {
            yield return new WaitForEndOfFrame();
            Texture2D texture = null;
            try
            {
                if (recording)
                {
                    if (frameCount >= 14400) { recordError = "frame_limit_reached"; StopRecording(); }
                    else
                    {
                        long timestamp = Now() - recordStarted;
                        texture = ScreenCapture.CaptureScreenshotAsTexture();
                        if (texture == null) throw new InvalidOperationException("capture_returned_no_texture");
                        lastCaptureWidth = texture.width; lastCaptureHeight = texture.height;
                        ValidateCaptureDimensions(lastCaptureWidth, lastCaptureHeight);
                        string relative = "frames/" + frameCount.ToString("D6", CultureInfo.InvariantCulture) + ".png";
                        File.WriteAllBytes(Path.Combine(recordPath, relative), ImageConversion.EncodeToPNG(texture));
                        frameIndex.WriteLine(new JObject { ["file"] = relative, ["elapsed_ms"] = timestamp, ["phase"] = phase, ["game_state_name"] = state, ["game_elapsed"] = Number(Read(manager, "SurvivedSeconds")), ["width"] = texture.width, ["height"] = texture.height }.ToString(Formatting.None));
                        frameCount++;
                    }
                }
            }
            catch (Exception ex) { recordError = ex.GetType().Name + ": " + ex.Message; StopRecording(); }
            finally { if (texture != null) Destroy(texture); capturePending = false; }
        }

        void StopRecording()
        {
            if (recording) recordEnded = Now();
            recording = false;
            if (frameIndex != null) { frameIndex.Dispose(); frameIndex = null; }
        }

        JObject RecordingStatus()
        {
            long now = Now();
            return new JObject { ["active"] = recording, ["path"] = recordPath ?? "", ["frames"] = frameCount, ["fps"] = recordFps, ["started_at_ms"] = recordStarted, ["now_ms"] = now, ["elapsed_ms"] = recordStarted == 0 ? 0 : (recording ? now : recordEnded) - recordStarted, ["screen_width"] = Screen.width, ["screen_height"] = Screen.height, ["last_capture_width"] = lastCaptureWidth, ["last_capture_height"] = lastCaptureHeight, ["error"] = recordError ?? "" };
        }

        static void ValidateCaptureDimensions(int width, int height)
        {
            if (width < 640 || height < 360)
                throw new InvalidOperationException("capture_resolution_too_small: " + width + "x" + height + "; minimum 640x360; restore a windowed game view before recording");
        }

        void OnApplicationQuit() { StopRecording(); Disconnect(); if (listener != null) listener.Stop(); }
        void OnDestroy() { StopRecording(); Disconnect(); if (listener != null) listener.Stop(); if (instance == this) instance = null; }

        static long Now() { return (long)(Time.realtimeSinceStartupAsDouble * 1000); }
        static double RequiredNumber(JObject request, string name)
        {
            var token = request[name];
            if (token == null || (token.Type != JTokenType.Float && token.Type != JTokenType.Integer)) throw new InvalidOperationException("invalid_" + name);
            double value = (double)token;
            if (double.IsNaN(value) || double.IsInfinity(value)) throw new InvalidOperationException("invalid_" + name);
            return value;
        }
        static bool Bool(object value) { return value is bool && (bool)value; }
        static double Number(object value) { if (value == null) return 0; try { double n = Convert.ToDouble(value, CultureInfo.InvariantCulture); return double.IsNaN(n) || double.IsInfinity(n) ? 0 : n; } catch { return 0; } }
        static IEnumerable<object> Items(object value) { var e = value as IEnumerable; if (e != null) foreach (var item in e) if (item != null) yield return item; }
        static Vector2 Vector(object value) { if (value is Vector2) return (Vector2)value; if (value is Vector3) return (Vector3)value; return new Vector2((float)Number(Read(value, "x")), (float)Number(Read(value, "y"))); }
        static Vector2 Position(object value) { var c = value as Component; return c != null ? (Vector2)c.transform.position : Vector2.zero; }
        static int ObjectId(object value) { var o = value as UnityEngine.Object; return o != null ? o.GetInstanceID() : 0; }
        static bool Alive(object value)
        {
            if (value == null) return false;
            var o = value as UnityEngine.Object;
            if (value is UnityEngine.Object && o == null) return false;
            var c = value as Component;
            if (c != null) return c.gameObject.activeInHierarchy && (!(c is Behaviour) || ((Behaviour)c).enabled);
            var go = value as GameObject;
            return go == null || go.activeInHierarchy;
        }
        static bool Interactable(object value)
        {
            var go = value as GameObject;
            if (go == null && value is Component) go = ((Component)value).gameObject;
            if (go == null || !go.activeInHierarchy) return false;
            foreach (var c in go.GetComponents<Component>())
                if (c != null && c.GetType().GetMethod("IsInteractable", Flags, null, Type.EmptyTypes, null) != null && !Bool(TryCall(c, "IsInteractable"))) return false;
            return true;
        }
        static double Radius(object value)
        {
            var c = value as Component;
            if (c == null) return 0.08;
            double bodyRadius = Number(Read(Read(value, "body"), "WorldRadius"));
            if (bodyRadius > 0) return bodyRadius;
            var collider = c.GetComponent<Collider2D>();
            return collider != null ? Math.Max(collider.bounds.extents.x, collider.bounds.extents.y) : Math.Max(0.03, Number(Read(value, "radius")));
        }
        static string TextOf(object value)
        {
            var go = value as GameObject;
            if (go == null && value is Component) go = ((Component)value).gameObject;
            if (go == null) return "";
            var texts = new List<string>();
            foreach (var c in go.GetComponentsInChildren<Component>(false))
            {
                if (c == null || c.GetType().Namespace != "TMPro") continue;
                string text = Convert.ToString(Read(c, "text"));
                if (!string.IsNullOrWhiteSpace(text) && !texts.Contains(text)) texts.Add(text);
            }
            string joined = string.Join(" | ", texts);
            return joined.Length > 1600 ? joined.Substring(0, 1600) : joined;
        }
        static Type GameType(string name)
        {
            Type type;
            if (!Types.TryGetValue(name, out type))
            {
                type = AppDomain.CurrentDomain.GetAssemblies().Select(a => a.GetType(name, false)).FirstOrDefault(t => t != null);
                if (type != null) Types[name] = type;
            }
            return type;
        }
        static object Find(string name) { var type = GameType(name); return type == null ? null : UnityEngine.Object.FindObjectOfType(type); }
        static object Read(object obj, string name)
        {
            if (obj == null) return null;
            Type type = obj as Type ?? obj.GetType();
            object target = obj is Type ? null : obj;
            for (Type t = type; t != null; t = t.BaseType)
            {
                var field = t.GetField(name, Flags | BindingFlags.DeclaredOnly);
                if (field != null) return field.GetValue(target);
                var property = t.GetProperty(name, Flags | BindingFlags.DeclaredOnly);
                if (property != null && property.GetIndexParameters().Length == 0) { try { return property.GetValue(target, null); } catch { return null; } }
            }
            return null;
        }
        static void Write(object obj, string name, object value)
        {
            for (Type t = obj.GetType(); t != null; t = t.BaseType)
            {
                var property = t.GetProperty(name, Flags | BindingFlags.DeclaredOnly);
                if (property != null && property.CanWrite) { property.SetValue(obj, value, null); return; }
                var field = t.GetField(name, Flags | BindingFlags.DeclaredOnly);
                if (field != null) { field.SetValue(obj, value); return; }
            }
            throw new MissingMemberException(name);
        }
        static object TryCall(object obj, string name) { try { return Call(obj, name); } catch { return null; } }
        static object Call(object obj, string name)
        {
            if (obj == null) throw new InvalidOperationException("missing_target");
            for (Type t = obj.GetType(); t != null; t = t.BaseType)
            {
                var method = t.GetMethod(name, Flags | BindingFlags.DeclaredOnly, null, Type.EmptyTypes, null);
                if (method != null) return method.Invoke(obj, null);
            }
            throw new MissingMethodException(name);
        }
    }
}
