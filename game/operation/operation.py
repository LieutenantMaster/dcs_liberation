from typing import Set

from gen import *
from gen.airfields import AIRFIELD_DATA
from gen.beacons import load_beacons_for_terrain
from gen.radios import RadioRegistry
from gen.tacan import TacanRegistry
from pydcs.dcs.countries import country_dict
from pydcs.dcs.lua.parse import loads
from pydcs.dcs.terrain.terrain import Terrain
from userdata.debriefing import *


class Operation:
    attackers_starting_position = None  # type: db.StartingPosition
    defenders_starting_position = None  # type: db.StartingPosition

    current_mission = None  # type: dcs.Mission
    regular_mission = None  # type: dcs.Mission
    quick_mission = None  # type: dcs.Mission
    conflict = None  # type: Conflict
    armorgen = None  # type: ArmorConflictGenerator
    airgen = None  # type: AircraftConflictGenerator
    triggersgen = None  # type: TriggersGenerator
    airsupportgen = None  # type: AirSupportConflictGenerator
    visualgen = None  # type: VisualGenerator
    envgen = None  # type: EnvironmentGenerator
    groundobjectgen = None  # type: GroundObjectsGenerator
    briefinggen = None  # type: BriefingGenerator
    forcedoptionsgen = None  # type: ForcedOptionsGenerator
    radio_registry: Optional[RadioRegistry] = None
    tacan_registry: Optional[TacanRegistry] = None

    environment_settings = None
    trigger_radius = TRIGGER_RADIUS_MEDIUM
    is_quick = None
    is_awacs_enabled = False
    ca_slots = 0

    def __init__(self,
                 game,
                 attacker_name: str,
                 defender_name: str,
                 from_cp: ControlPoint,
                 departure_cp: ControlPoint,
                 to_cp: ControlPoint = None):
        self.game = game
        self.attacker_name = attacker_name
        self.attacker_country = db.FACTIONS[attacker_name]["country"]
        self.defender_name = defender_name
        self.defender_country = db.FACTIONS[defender_name]["country"]
        print(self.defender_country, self.attacker_country)
        self.from_cp = from_cp
        self.departure_cp = departure_cp
        self.to_cp = to_cp
        self.is_quick = False

    def units_of(self, country_name: str) -> typing.Collection[UnitType]:
        return []

    def is_successfull(self, debriefing: Debriefing) -> bool:
        return True

    @property
    def is_player_attack(self) -> bool:
        return self.from_cp.captured

    def initialize(self, mission: Mission, conflict: Conflict):
        self.current_mission = mission
        self.conflict = conflict
        self.radio_registry = RadioRegistry()
        self.tacan_registry = TacanRegistry()
        self.airgen = AircraftConflictGenerator(
            mission, conflict, self.game.settings, self.game,
            self.radio_registry)
        self.airsupportgen = AirSupportConflictGenerator(
            mission, conflict, self.game, self.radio_registry,
            self.tacan_registry)
        self.triggersgen = TriggersGenerator(mission, conflict, self.game)
        self.visualgen = VisualGenerator(mission, conflict, self.game)
        self.envgen = EnviromentGenerator(mission, conflict, self.game)
        self.forcedoptionsgen = ForcedOptionsGenerator(mission, conflict, self.game)
        self.groundobjectgen = GroundObjectsGenerator(
            mission,
            conflict,
            self.game,
            self.radio_registry,
            self.tacan_registry
        )
        self.briefinggen = BriefingGenerator(mission, conflict, self.game)

    def prepare(self, terrain: Terrain, is_quick: bool):
        with open("resources/default_options.lua", "r") as f:
            options_dict = loads(f.read())["options"]

        self.current_mission = dcs.Mission(terrain)

        print(self.game.player_country)
        print(country_dict[db.country_id_from_name(self.game.player_country)])
        print(country_dict[db.country_id_from_name(self.game.player_country)]())

        # Setup coalition :
        self.current_mission.coalition["blue"] = Coalition("blue")
        self.current_mission.coalition["red"] = Coalition("red")

        p_country = self.game.player_country
        e_country = self.game.enemy_country
        self.current_mission.coalition["blue"].add_country(country_dict[db.country_id_from_name(p_country)]())
        self.current_mission.coalition["red"].add_country(country_dict[db.country_id_from_name(e_country)]())

        print([c for c in self.current_mission.coalition["blue"].countries.keys()])
        print([c for c in self.current_mission.coalition["red"].countries.keys()])

        if is_quick:
            self.quick_mission = self.current_mission
        else:
            self.regular_mission = self.current_mission

        self.current_mission.options.load_from_dict(options_dict)
        self.is_quick = is_quick

        if is_quick:
            self.attackers_starting_position = None
            self.defenders_starting_position = None
        else:
            self.attackers_starting_position = self.departure_cp.at
            self.defenders_starting_position = self.to_cp.at

    def generate(self):
        # Dedup beacon frequencies, since some maps have more than one beacon
        # per frequency.
        beacons = load_beacons_for_terrain(self.game.theater.terrain.name)
        unique_beacon_frequencies: Set[RadioFrequency] = set()
        for beacon in beacons:
            unique_beacon_frequencies.add(beacon.frequency)
            if beacon.is_tacan:
                if beacon.channel is None:
                    logging.error(
                        f"TACAN beacon has no channel: {beacon.callsign}")
                else:
                    self.tacan_registry.reserve(beacon.tacan_channel)
        for frequency in unique_beacon_frequencies:
            self.radio_registry.reserve(frequency)

        for airfield, data in AIRFIELD_DATA.items():
            if data.theater == self.game.theater.terrain.name:
                self.radio_registry.reserve(data.atc.hf)
                self.radio_registry.reserve(data.atc.vhf_fm)
                self.radio_registry.reserve(data.atc.vhf_am)
                self.radio_registry.reserve(data.atc.uhf)
                # No need to reserve ILS or TACAN because those are in the
                # beacon list.

        # Generate meteo
        if self.environment_settings is None:
            self.environment_settings = self.envgen.generate()
        else:
            self.envgen.load(self.environment_settings)

        # Generate ground object first
        self.groundobjectgen.generate()

        # Generate destroyed units
        for d in self.game.get_destroyed_units():
            try:
                utype = db.unit_type_from_name(d["type"])
            except KeyError:
                continue

            pos = Point(d["x"], d["z"])
            if utype is not None and not self.game.position_culled(pos) and self.game.settings.perf_destroyed_units:
                self.current_mission.static_group(
                    country=self.current_mission.country(self.game.player_country),
                    name="",
                    _type=utype,
                    hidden=True,
                    position=pos,
                    heading=d["orientation"],
                    dead=True,
                )


        # Air Support (Tanker & Awacs)
        self.airsupportgen.generate(self.is_awacs_enabled)

        # Generate Activity on the map
        for cp in self.game.theater.controlpoints:
            side = cp.captured
            if side:
                country = self.current_mission.country(self.game.player_country)
            else:
                country = self.current_mission.country(self.game.enemy_country)
            if cp.id in self.game.planners.keys():
                self.airgen.generate_flights(
                    cp,
                    country,
                    self.game.planners[cp.id],
                    self.groundobjectgen.runways
                )

        # Generate ground units on frontline everywhere
        self.game.jtacs = []
        for player_cp, enemy_cp in self.game.theater.conflicts(True):
            conflict = Conflict.frontline_cas_conflict(self.attacker_name, self.defender_name,
                                                       self.current_mission.country(self.attacker_country),
                                                       self.current_mission.country(self.defender_country),
                                                       player_cp, enemy_cp, self.game.theater)
            # Generate frontline ops
            player_gp = self.game.ground_planners[player_cp.id].units_per_cp[enemy_cp.id]
            enemy_gp = self.game.ground_planners[enemy_cp.id].units_per_cp[player_cp.id]
            groundConflictGen = GroundConflictGenerator(self.current_mission, conflict, self.game, player_gp, enemy_gp, player_cp.stances[enemy_cp.id])
            groundConflictGen.generate()

        # Setup combined arms parameters
        self.current_mission.groundControl.pilot_can_control_vehicles = self.ca_slots > 0
        if self.game.player_country in [country.name for country in self.current_mission.coalition["blue"].countries.values()]:
            self.current_mission.groundControl.blue_tactical_commander = self.ca_slots
        else:
            self.current_mission.groundControl.red_tactical_commander = self.ca_slots

        # Triggers
        if self.game.is_player_attack(self.conflict.attackers_country):
            cp = self.conflict.from_cp
        else:
            cp = self.conflict.to_cp
        self.triggersgen.generate()

        # Options
        self.forcedoptionsgen.generate()

        # Generate Visuals Smoke Effects
        if self.game.settings.perf_smoke_gen:
            self.visualgen.generate()

        # Inject Lua Scripts
        load_mist = TriggerStart(comment="Load Mist Lua Framework")
        with open("./resources/scripts/mist_4_3_74.lua") as f:
            load_mist.add_action(DoScript(String(f.read())))
        self.current_mission.triggerrules.triggers.append(load_mist)

        # Load Ciribob's JTACAutoLase script
        load_autolase = TriggerStart(comment="Load JTAC script")
        with open("./resources/scripts/JTACAutoLase.lua") as f:

            script = f.read()
            script = script + "\n"

            smoke = "true"
            if hasattr(self.game.settings, "jtac_smoke_on"):
                if not self.game.settings.jtac_smoke_on:
                    smoke = "false"

            for jtac in self.game.jtacs:
                script = script + "\n" + "JTACAutoLase('" + str(jtac[2]) + "', " + str(jtac[1]) + ", " + smoke + ", \"vehicle\")" + "\n"

            load_autolase.add_action(DoScript(String(script)))
        self.current_mission.triggerrules.triggers.append(load_autolase)

        load_dcs_libe = TriggerStart(comment="Load DCS Liberation Script")
        with open("./resources/scripts/dcs_liberation.lua") as f:
            script = f.read()
            json_location = "[["+os.path.abspath("resources\\scripts\\json.lua")+"]]"
            state_location = "[[" + os.path.abspath("state.json") + "]]"
            script = script.replace("{{json_file_abs_location}}", json_location)
            script = script.replace("{{debriefing_file_location}}", state_location)
            load_dcs_libe.add_action(DoScript(String(script)))
        self.current_mission.triggerrules.triggers.append(load_dcs_libe)

        kneeboard_generator = KneeboardGenerator(self.current_mission)

        # Briefing Generation
        for tanker in self.airsupportgen.air_support.tankers:
            self.briefinggen.append_frequency(
                f"Tanker {tanker.callsign} ({tanker.variant})",
                f"{tanker.tacan}/{tanker.freq}")
            kneeboard_generator.add_tanker(tanker)

        if self.is_awacs_enabled:
            for awacs in self.airsupportgen.air_support.awacs:
                self.briefinggen.append_frequency(awacs.callsign, awacs.freq)
                kneeboard_generator.add_awacs(awacs)

        self.assign_channels_to_flights()

        # Generate the briefing
        self.briefinggen.generate()

        for region, code, name in self.game.jtacs:
            kneeboard_generator.add_jtac(name, region, code)
        kneeboard_generator.generate(self.airgen.flights)

    def assign_channels_to_flights(self) -> None:
        """Assigns preset radio channels for client flights."""
        for flight in self.airgen.flights:
            if not flight.client_units:
                continue
            self.assign_channels_to_flight(flight)

    def assign_channels_to_flight(self, flight: FlightData) -> None:
        """Assigns preset radio channels for a client flight."""
        airframe = flight.aircraft_type

        try:
            aircraft_data = AIRCRAFT_DATA[airframe.id]
        except KeyError:
            logging.warning(f"No aircraft data for {airframe.id}")
            return

        # Intra-flight channel is set up when the flight is created, however we
        # do need to make sure we don't overwrite it. For cases where the
        # inter-flight and intra-flight radios share presets (the AV-8B only has
        # one set of channels, even though it can use two channels
        # simultaneously), start assigning channels at 2.
        radio_id = aircraft_data.inter_flight_radio_index
        if aircraft_data.intra_flight_radio_index == radio_id:
            first_channel = 2
        else:
            first_channel = 1

        last_channel = flight.num_radio_channels(radio_id)
        channel_alloc = iter(range(first_channel, last_channel + 1))

        flight.assign_channel(radio_id, next(channel_alloc),flight.departure.atc)

        # TODO: If there ever are multiple AWACS, limit to mission relevant.
        for awacs in self.airsupportgen.air_support.awacs:
            flight.assign_channel(radio_id, next(channel_alloc), awacs.freq)

        # TODO: Fix departure/arrival to support carriers.
        if flight.arrival != flight.departure:
            flight.assign_channel(radio_id, next(channel_alloc),
                                  flight.arrival.atc)

        try:
            # TODO: Skip incompatible tankers.
            for tanker in self.airsupportgen.air_support.tankers:
                flight.assign_channel(
                    radio_id, next(channel_alloc), tanker.freq)

            if flight.divert is not None:
                flight.assign_channel(radio_id, next(channel_alloc),
                                      flight.divert.atc)
        except StopIteration:
            # Any remaining channels are nice-to-haves, but not necessary for
            # the few aircraft with a small number of channels available.
            pass
