# -*- coding: utf-8 -*-
"""
Moves a trainer to a Pokemon.

Events:
    move_to_map_pokemon
        When a generic message is logged
        Returns:
            message: Log message.

    move_to_map_pokemon_fail
        When the worker fails.
        Returns:
            message: Failure message.

    move_to_map_pokemon_updated_map
        When worker updates the PokemonGo-Map.
        Returns:
            lat: Latitude
            lon: Longitude

    move_to_map_pokemon_teleport_to
        When trainer is teleported to a Pokemon.
        Returns:
            poke_name: Pokemon's name
            poke_dist: Distance from the trainer
            poke_lat: Latitude of the Pokemon
            poke_lon: Longitude of the Pokemon
            disappears_in: Number of seconds before the Pokemon disappears

    move_to_map_pokemon_encounter
        When a trainer encounters a Pokemon by teleporting or walking.
        Returns:
            poke_name: Pokemon's name
            poke_dist: Distance from the trainer
            poke_lat: Latitude of the Pokemon
            poke_lon: Longitude of the Pokemon
            disappears_in: Number of seconds before the Pokemon disappears

    move_to_map_pokemon_move_towards
        When a trainer moves toward a Pokemon.
        Returns:
            poke_name: Pokemon's name
            poke_dist: Distance from the trainer
            poke_lat: Latitude of the Pokemon
            poke_lon: Longitude of the Pokemon
            disappears_in: Number of seconds before the Pokemon disappears

    move_to_map_pokemon_teleport_back
        When a trainer teleports back to thier previous location.
        Returns:
            last_lat: Trainer's last known latitude
            last_lon: Trainer's last known longitude

"""

from __future__ import unicode_literals

import os
import time
import json
import requests

import re
import Queue
import threading
from itertools import groupby
from socketIO_client import SocketIO, BaseNamespace

from pokemongo_bot import inventory
from pokemongo_bot.base_dir import _base_dir
from pokemongo_bot.cell_workers.utils import distance, format_dist, format_time, fort_details
from pokemongo_bot.walkers.walker_factory import walker_factory
from pokemongo_bot.worker_result import WorkerResult
from pokemongo_bot.base_task import BaseTask
from pokemongo_bot.cell_workers.pokemon_catch_worker import PokemonCatchWorker
from random import uniform, shuffle
from pokemongo_bot.constants import Constants
from datetime import datetime

ULTRABALL_ID = 3
GREATBALL_ID = 2
POKEBALL_ID = 1

def tryFloat(val, defVal = 0):
   try:
     return float(val)
   except ValueError:
     return defVal

class MoveToMapPokemon(BaseTask):
    """Task for moving a trainer to a Pokemon."""
    SUPPORTED_TASK_API_VERSION = 1

    def initialize(self):
        self.hj_mode = self.config.get('hj_mode', False)
        self.iv_catch = self.config.get('iv_catch', 0.99)
        self.iv_keep = self.config.get('iv_keep', 2)
        self.powerup_candy = self.config.get('powerup_candy', 20)
        if self.hj_mode == 'pokezz':
            self.pokezz_list = []
            self.pokezz()
        elif self.hj_mode == 'pkget' or self.hj_mode == 'poke5566':
            self.locs = self.config.get('locs', [])
            shuffle(self.locs)
            self.session = None
        self.hj_switch = 0
        self.last_map_update = 0
        self.pokemon_data = self.bot.pokemon_list
        self.unit = self.bot.config.distance_unit
        self.cache = []
        self.min_ball = self.config.get('min_ball', 1)
        self.map_path = self.config.get('map_path', 'raw_data')
        self.walker = self.config.get('walker', 'StepWalker')
        self.snip_enabled = self.config.get('snipe', False)
        self.snipe_high_prio_only = self.config.get('snipe_high_prio_only', False)
        self.snipe_high_prio_threshold = self.config.get('snipe_high_prio_threshold', 400)
        self.by_pass_times = 0

        data_file = os.path.join(_base_dir, 'map-caught-{}.json'.format(self.bot.config.username))
        if os.path.isfile(data_file):
            self.cache = json.load(
                open(data_file)
            )
        self.alt = uniform(self.bot.config.alt_min, self.bot.config.alt_max)
        self.debug = self.config.get('debug', False)

    def pokezz(self):
      self.pokezz_sock = SocketIO(
        host='https://pokezz.com',
        verify=False,
        Namespace=BaseNamespace,
        headers={
          'Referer': 'https://pokezz.com/',
          'Host': 'pokezz.com',
          'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/52.0.2743.116 Safari/537.36',
          'Origin': 'https://pokezz.com',
        }
      )
      self.pokezz_sock.on('b', self.pokezz_noti)
      self.pokezz_thread = threading.Thread(target=self.pokezz_thread_process)
      self.pokezz_thread.start()

    def pokezz_thread_process(self):
      self.pokezz_sock.wait()

    def pokezz_noti(self, data):
      m = data.split('|')
      if m[4] != '1':
        return

      self.pokezz_list.append({
        'pokemon_id': int(m[0]),
        'name': self.pokemon_data[int(m[0]) - 1]['Name'],
        'latitude': float(m[1]),
        'longitude': float(m[2]),
        'disappear_time': int(tryFloat(m[3]))*1000,
        'iv': tryFloat(m[6]),
      })

    def pokemons_parser(self, pokemon_list):
        pokemons = []
        if not pokemon_list:
            return pokemons

        now = int(time.time())

        for pokemon in pokemon_list:
            try:
                disappear = int(pokemon.get('expiration_timestamp_ms', 0) / 1000) or int(pokemon.get('disappear_time', 0) / 1000)

                pokemon['encounter_id'] = pokemon.get('encounter_id', '')
                pokemon['spawn_point_id'] = pokemon.get('spawn_point_id', '') or pokemon.get('spawnpoint_id', '')
                pokemon['iv'] = pokemon.get('iv', 0)
                pokemon['disappear_time'] = disappear
                pokemon['name'] = self.pokemon_data[pokemon['pokemon_id'] - 1]['Name']
                pokemon['is_vip'] = pokemon['name'] in self.bot.config.vips

                if self.bot.config.unique_catch:
                    if inventory.pokemons().count(pokemon['pokemon_id']) > 0:
                        continue
                    pokemon['is_vip'] = True

            except TypeError:
                continue
            except KeyError:
                continue
            if now > pokemon['disappear_time']:
                continue

            if pokemon['name'] not in self.config['catch'] and not pokemon['is_vip']:
                if self.debug:
                    self._emit_failure("Not catching {}".format(pokemon['name']))
                continue

            if self.is_inspected(pokemon):
                if self.debug:
                    self._emit_log('Skipped {} because it was already catch or does not exist'.format(pokemon['name']))
                continue

            pokemon['priority'] = self.config['catch'].get(pokemon['name'], 0)
            pokemon['dist'] = distance(
                self.bot.position[0],
                self.bot.position[1],
                pokemon['latitude'],
                pokemon['longitude']
            )

            # If distance to pokemon greater than the max_sniping_distance, then ignore regardless of "snipe" setting
            if pokemon['dist'] > self.config.get('max_sniping_distance', 10000):
                continue

            # If distance bigger than walking distance, ignore if sniping is not active
            if pokemon['dist'] > self.config.get('max_walking_distance', 1000) and not self.snip_enabled:
                continue

            # if pokemon not reachable with mean walking speed (by config)
            mean_walk_speed = (self.bot.config.walk_max + self.bot.config.walk_min) / 2
            if pokemon['dist'] > ((pokemon['disappear_time'] - now) * mean_walk_speed) and not self.snip_enabled:
                continue
            pokemons.append(pokemon)

        return pokemons

    def hj_filter(self, pokemon_list):
        if not len(pokemon_list):
            return pokemon_list

        family = {}
        for p in inventory.pokemons().STATIC_DATA:
            fid = p.first_evolution_id
            if not family.has_key(fid):
                family[fid] = {'candy':-1, 'iv':0, 'catch_candy':False, 'catch_iv':False}

        for p in inventory.pokemons().all():
            fid = p.first_evolution_id
            if family[fid]['candy'] == -1:
                family[fid]['candy'] = p.candy_quantity

            if p.iv > self.iv_catch:
                family[fid]['iv'] += 1
                evolve_pid = p.pokemon_id
                while inventory.pokemons().has_next_evolution(evolve_pid):
                    family[fid]['candy'] -= inventory.pokemons().evolution_cost_for(evolve_pid)
                    evolve_pid = inventory.pokemons().next_evolution_ids_for(evolve_pid)[0]

            iv_keep = (self.iv_keep*3) if p.pokemon_id == 133 else self.iv_keep
            family[fid]['catch_iv'] = family[fid]['iv'] < iv_keep
            family[fid]['catch_candy'] = family[fid]['candy'] < self.powerup_candy

        catch_list = []
        for p in pokemon_list:
            fid = inventory.pokemons().first_evolution_id_for(p['pokemon_id'])
            if p['iv'] > self.iv_catch*100:
                if family[fid]['catch_iv']:
                    p['is_vip'] = True
                    catch_list.append(p)
            else:
                if family[fid]['catch_candy']:
                    catch_list.append(p)

        return catch_list

    def get_pokemon_from_pokezz(self):
        if not self.hj_mode:
            return []

        tmp_pokemon_list, self.pokezz_list = self.pokezz_list, []
        olen = len(tmp_pokemon_list)

        tmp_pokemon_list.sort(key=lambda x: -x['iv'])
        groups = []
        for k, g in groupby(tmp_pokemon_list, lambda x: x['pokemon_id']):
            for t in g:
                groups.append(t)
                tm = time.strftime('%H:%M:%S', time.localtime(int(t['disappear_time'])/1000))
                self._emit_log("%s %s%% %s (%s,%s)" % (t['name'],t['iv'],tm,t['latitude'],t['longitude']))
                break
        tmp_pokemon_list = groups

        tmp_pokemon_list = filter(lambda x: x["iv"] >= 99, tmp_pokemon_list)
        self._emit_log(" ==>> from pokezz: %s/%s" % (len(tmp_pokemon_list),olen))
        return self.pokemons_parser(tmp_pokemon_list)

    def get_pokemon_from_pokesniper(self):
        if not self.hj_mode:
            return []

        pokemon = ["", "Bulbasaur", "Ivysaur", "Venusaur", "Charmander", "Charmeleon", "Charizard", "Squirtle", "Wartortle", "Blastoise", "Caterpie", "Metapod", "Butterfree", "Weedle", "Kakuna", "Beedrill", "Pidgey", "Pidgeotto", "Pidgeot", "Rattata", "Raticate", "Spearow", "Fearow", "Ekans", "Arbok", "Pikachu", "Raichu", "Sandshrew", "Sandslash", "NidoranF", "Nidorina", "Nidoqueen", "NidoranM", "Nidorino", "Nidoking", "Clefairy", "Clefable", "Vulpix", "Ninetales", "Jigglypuff", "Wigglytuff", "Zubat", "Golbat", "Oddish", "Gloom", "Vileplume", "Paras", "Parasect", "Venonat", "Venomoth", "Diglett", "Dugtrio", "Meowth", "Persian", "Psyduck", "Golduck", "Mankey", "Primeape", "Growlithe", "Arcanine", "Poliwag", "Poliwhirl", "Poliwrath", "Abra", "Kadabra", "Alakazam", "Machop", "Machoke", "Machamp", "Bellsprout", "Weepinbell", "Victreebel", "Tentacool", "Tentacruel", "Geodude", "Graveler", "Golem", "Ponyta", "Rapidash", "Slowpoke", "Slowbro", "Magnemite", "Magneton", "Farfetch'd", "Doduo", "Dodrio", "Seel", "Dewgong", "Grimer", "Muk", "Shellder", "Cloyster", "Gastly", "Haunter", "Gengar", "Onix", "Drowzee", "Hypno", "Krabby", "Kingler", "Voltorb", "Electrode", "Exeggcute", "Exeggcutor", "Cubone", "Marowak", "Hitmonlee", "Hitmonchan", "Lickitung", "Koffing", "Weezing", "Rhyhorn", "Rhydon", "Chansey", "Tangela", "Kangaskhan", "Horsea", "Seadra", "Goldeen", "Seaking", "Staryu", "Starmie", "Mr. Mime", "Scyther", "Jynx", "Electabuzz", "Magmar", "Pinsir", "Tauros", "Magikarp", "Gyarados", "Lapras", "Ditto", "Eevee", "Vaporeon", "Jolteon", "Flareon", "Porygon", "Omanyte", "Omastar", "Kabuto", "Kabutops", "Aerodactyl", "Snorlax", "Articuno", "Zapdos", "Moltres", "Dratini", "Dragonair", "Dragonite", "Mewtwo", "Mew"];

        url = 'http://pokesniper.org/newapiman.txt'
        try:
            request = requests.get(url)
            response = request.json()
        except requests.exceptions.ConnectionError:
            self._emit_failure('Could not get data from {}'.format(url))
            return []
        except ValueError:
            self._emit_failure('JSON format is not valid')
            return []

        olen = len(response)
        tmp_pokemon_list = []
        now = (time.time()+600) * 1000
        for p in response:
          try:
            tmp_pokemon_list.append({
                'pokemon_id': int(pokemon.index(p['name'])),
                'name': p['name'],
                'latitude': float(p['coords'].split(',')[0]),
                'longitude': float(p['coords'].split(',')[1]),
                'disappear_time': int(now),
                'iv': tryFloat(p['iv']),
            })
          except:
            self._emit_log(" wrong: %s" % p['name'])
            pass

        tmp_pokemon_list = filter(lambda x: x["iv"] >= 99, tmp_pokemon_list)
        self._emit_log(" ==>> from pokesniper: %s/%s" % (len(tmp_pokemon_list),olen))
        return self.pokemons_parser(tmp_pokemon_list)

    def get_pokemon_from_poke5566(self):
        if not self.hj_mode:
            return []
        if not self.locs:
            return []

        url0 = 'https://poke5566.com/'
        if not self.session:
            self.session = requests.Session()
            self.session.headers.update({
              'Referer': url0,
              'X-Requested-With': 'XMLHttpRequest',
              'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/53.0.2785.116 Safari/537.36'
            })
            try:
                self.session.get(url0)
            except requests.exceptions.ConnectionError:
                self.session = None
                self._emit_failure('Could not init from {}'.format(url0))
                return []

        url = 'https://poke5566.com/pokemons'
        loc = self.locs.pop(0)
        self.locs.append(loc)
        llen = 0.05
        params = {
            'lat0': loc[0] + llen,
            'lng0': loc[1] + llen,
            'lat1': loc[0] - llen,
            'lng1': loc[1] - llen
        }
        cookies = {
            'star':'3',
            'iv':'80',
            '_ga':'GA1.2.93737906.1475208123',
            'poke5566':"lat0={lat0}&lng0={lng0}&lat1={lat1}&lng1={lng1}".format(**params)
        }
        try:
            self.session.cookies.update(cookies)
            request = self.session.get(url, params=params)
            response = request.json()
        except requests.exceptions.ConnectionError:
            self._emit_failure('Could not get data from {}'.format(url))
            return []
        except ValueError:
            self._emit_failure('JSON format is not valid')
            return []

        response = response.get('pokemons', [])
        olen = len(response)
        tmp_pokemon_list = [{
            'pokemon_id': int(p['i']),
            'name': self.pokemon_data[int(p['i']) - 1]['Name'],
            'latitude': float(p['a']),
            'longitude': float(p['n']),
            'disappear_time': int(tryFloat(p['t'])),
            'iv': p['v'][0] and tryFloat((p['v'][0]+p['v'][1]+p['v'][2])/45.*100) or 10.0,
        } for p in response]

        self._emit_log(" ==>> from poke5566: %s/%s, %s" % (len(tmp_pokemon_list), olen, loc[2]))
        return self.pokemons_parser(tmp_pokemon_list)

    def get_pokemon_from_pkget(self):
        if not self.hj_mode:
            return []
        if not self.locs:
            return []

        url = 'https://pkget.com/pkm333.ashx'
        loc = self.locs.pop(0)
        self.locs.append(loc)
        llen = 0.05
        params = {
            'v1': 111,
            'v2': loc[0] + llen,
            'v3': loc[1] + llen,
            'v4': loc[0] - llen,
            'v5': loc[1] - llen,
            'v6': 0
        }
        headers = {
            'X-Requested-With': 'XMLHttpRequest',
            'Referer': 'https://pkget.com/',
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_12_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/53.0.2785.116 Safari/537.36'
        }
        try:
            request = requests.get(url, params=params, headers=headers)
            response = request.json()
        except requests.exceptions.ConnectionError:
            self._emit_failure('Could not get data from {}'.format(url))
            return []
        except ValueError:
            self._emit_failure('JSON format is not valid')
            return []

        response = response.get('pk123', [])
        olen = len(response)
        tmp_pokemon_list = [{
            'pokemon_id': int(p['d1']),
            'name': self.pokemon_data[int(p['d1']) - 1]['Name'],
            'latitude': float(p['d4']),
            'longitude': float(p['d5']),
            'disappear_time': int(tryFloat(p['d3'])),
            'iv': tryFloat(p['d9'].split('^')[5]),
        } for p in response]
        #tmp_pokemon_list = filter(lambda x: x["iv"] >= 90, tmp_pokemon_list)
        #tmp_pokemon_list.sort(key=lambda x: -x['iv'])

        self._emit_log(" ==>> from pkget: %s/%s, %s" % (len(tmp_pokemon_list), olen, loc[2]))
        return self.pokemons_parser(tmp_pokemon_list)

    def get_pokemon_from_social(self):
        if not hasattr(self.bot, 'mqtt_pokemon_list') or not self.bot.mqtt_pokemon_list:
            return []

        tmp_pokemon_list, self.bot.mqtt_pokemon_list = self.bot.mqtt_pokemon_list, []
        self._emit_log(" ==>> from social: %s" % len(tmp_pokemon_list))
        return self.pokemons_parser(tmp_pokemon_list)

    def get_pokemon_from_url(self):
        try:
            request = requests.get(self.config['address'])
            response = request.json()
        except requests.exceptions.ConnectionError:
            self._emit_failure('Could not get data from {}'.format(self.config['address']))
            return []
        except ValueError:
            self._emit_failure('JSON format is not valid')
            return []

        tmp_pokemon_list = response.get('pokemons', [])
        return self.pokemons_parser(tmp_pokemon_list)

    # TODO: refactor
    def is_inspected(self, pokemon):
        for caught_pokemon in self.cache:
            # Since IDs might be invalid (null/blank) by this time, compare by approximate location
            # TODO: make a better comparision
            same_latitude = "{0:.4f}".format(pokemon['latitude']) == "{0:.4f}".format(caught_pokemon['latitude'])
            same_longitude = "{0:.4f}".format(pokemon['longitude']) == "{0:.4f}".format(caught_pokemon['longitude'])
            if same_latitude and same_longitude:
                return True

        return False

    # Stores a target so that
    # TODO: refactor
    def inspect(self, pokemon):
        # Make sure it was not caught!
        for caught_pokemon in self.cache:
            same_latitude = "{0:.4f}".format(pokemon['latitude']) == "{0:.4f}".format(caught_pokemon['latitude'])
            same_longitude = "{0:.4f}".format(pokemon['longitude']) == "{0:.4f}".format(caught_pokemon['longitude'])
            if same_latitude and same_longitude:
                return

        if len(self.cache) >= 200:
            self.cache.pop(0)

        self.cache.append(pokemon)

    def snipe(self, pokemon):
        # Backup position before anything
        last_position = self.bot.position[0:2]

        # Teleport, so that we can see nearby stuff
        # self.bot.heartbeat() was moved to thread, if you do want to call it, you need sleep 10s.
        self.bot.hb_locked = True
        self._teleport_to(pokemon)

        # Simulate kind of a lag after teleporting/moving to a long distance
        time.sleep(2)

        # If social is enabled and if no verification is needed, trust it. Otherwise, update IDs!
        verify = not pokemon.get('encounter_id') or not pokemon.get('spawn_point_id')
        exists = not verify and self.bot.config.enable_social

        # If social is disabled, we will have to make sure the target still exists
        if verify:
            # Sleep some time, so that we have accurate results (successfull cell data request)
            time.sleep(5)

            nearby_pokemons = []
            nearby_stuff = self.bot.get_meta_cell()

            # Retrieve nearby pokemons for validation
            if 'wild_pokemons' in nearby_stuff:
                nearby_pokemons.extend(nearby_stuff['wild_pokemons'])
            if 'catchable_pokemons' in nearby_stuff:
                nearby_pokemons.extend(nearby_stuff['catchable_pokemons'])

            # Make sure the target still/really exists (TODO: validate expiration)
            for nearby_pokemon in nearby_pokemons:
                is_wild = 'pokemon_data' in nearby_pokemon
                nearby_pokemon_id = nearby_pokemon['pokemon_data']['pokemon_id'] if is_wild else nearby_pokemon['pokemon_id']

                if nearby_pokemon_id == pokemon['pokemon_id']:
                    exists = True

                    # Also, if the IDs arent valid, update them!
                    if not pokemon['encounter_id'] or not pokemon['spawn_point_id']:
                        pokemon['encounter_id'] = nearby_pokemon['encounter_id']
                        pokemon['spawn_point_id'] = nearby_pokemon['spawn_point_id']
                        pokemon['disappear_time'] = nearby_pokemon['last_modified_timestamp_ms'] if is_wild else nearby_pokemon['expiration_timestamp_ms']
                    break

        # If target exists, catch it, otherwise ignore
        if exists:
            self._encountered(pokemon)
            catch_worker = PokemonCatchWorker(pokemon, self.bot)
            api_encounter_response = catch_worker.create_encounter_api_call()
            time.sleep(self.config.get('snipe_sleep_sec', 2))
            self._teleport_back(last_position)
            self.bot.api.set_position(last_position[0], last_position[1], self.alt, False)
            time.sleep(self.config.get('snipe_sleep_sec', 2))
            catch_worker.work(api_encounter_response)
        else:
            self._emit_failure('{} doesnt exist anymore. Skipping...'.format(pokemon['name']))
            time.sleep(self.config.get('snipe_sleep_sec', 2))
            self._teleport_back(last_position)
            self.bot.api.set_position(last_position[0], last_position[1], self.alt, False)
            time.sleep(self.config.get('snipe_sleep_sec', 2))

        self.inspect(pokemon)
        self.bot.hb_locked = False
        return WorkerResult.SUCCESS

    def dump_caught_pokemon(self):
        user_data_map_caught = os.path.join(_base_dir, 'data', 'map-caught-{}.json'.format(self.bot.config.username))
        with open(user_data_map_caught, 'w') as outfile:
            json.dump(self.cache, outfile)

    def work(self):
        if self.bot.last_catch_cooldown > time.time():
            return WorkerResult.SUCCESS

        # check for pokeballs (excluding masterball)
        pokeballs_quantity = inventory.items().get(POKEBALL_ID).count
        superballs_quantity = inventory.items().get(GREATBALL_ID).count
        ultraballs_quantity = inventory.items().get(ULTRABALL_ID).count

        # Validate the balls quantity
        if (pokeballs_quantity + superballs_quantity + ultraballs_quantity) < self.min_ball:
            if self.debug:
                self._emit_log("Not enough balls to start sniping (have {}, {} needed)".format(
                    pokeballs_quantity + superballs_quantity + ultraballs_quantity, self.min_ball))
            return WorkerResult.SUCCESS
            
        if self.bot.catch_disabled:
            if not hasattr(self.bot,"mtmp_disabled_global_warning") or \
                        (hasattr(self.bot,"mtmp_disabled_global_warning") and not self.bot.mtmp_disabled_global_warning):
                self._emit_log("All catching tasks are currently disabled until {}. Sniping will resume when catching tasks are re-enabled".format(self.bot.catch_resume_at.strftime("%H:%M:%S")))
            self.bot.mtmp_disabled_global_warning = True
            return WorkerResult.SUCCESS
        else:
            self.bot.mtmp_disabled_global_warning = False

        # Retrieve pokemos
        self.dump_caught_pokemon()
        if self.bot.config.enable_social:
            if self.snip_enabled:
                self.by_pass_times += 1
                if self.by_pass_times < self.config.get('skip_rounds', 30):
                    if self.debug:
                        self._emit_log("Skipping pass {}".format(self.by_pass_times))
                    return WorkerResult.SUCCESS
                self.by_pass_times = 0
            if self.hj_mode:
                self.hj_switch += 1
            if self.hj_switch % 10 == 0:
                pokemon_list = self.get_pokemon_from_social()
            elif self.hj_switch % 2 == 1:
                pokemon_list = self.get_pokemon_from_pokesniper()
            elif self.hj_mode == 'pokezz':
                pokemon_list = self.get_pokemon_from_pokezz()
            elif self.hj_mode == 'pkget':
                pokemon_list = self.get_pokemon_from_pkget()
            elif self.hj_mode == 'poke5566':
                pokemon_list = self.get_pokemon_from_poke5566()
        else:
            pokemon_list = self.get_pokemon_from_url()

        if self.hj_mode:
            pokemon_list = self.hj_filter(pokemon_list)

        if not self.hj_mode:
            pokemon_list.sort(key=lambda x: x['dist'])
        if self.config['mode'] == 'priority':
            pokemon_list.sort(key=lambda x: x['priority'], reverse=True)
        if self.config['prioritize_vips']:
            pokemon_list.sort(key=lambda x: x['is_vip'], reverse=True)

        if not len(pokemon_list):
            if self.debug:
                self._emit_log("No pokemons in list to snipe")
            return WorkerResult.SUCCESS

        pokemon = pokemon_list[0]
        if self.debug:
            self._emit_log('How many pokemon in list: {}'.format(len(pokemon_list)))

        if self.snip_enabled:
            if self.snipe_high_prio_only:
                count = 0
                for pokemon in pokemon_list:
                    if self.snipe_high_prio_threshold < pokemon['priority']:
                        self.snipe(pokemon)
                        count += 1
                        if count >= self.config.get('snipe_max_in_chain', 2):
                            return WorkerResult.SUCCESS
                        if count is not 1:
                            time.sleep(self.config.get('snipe_sleep_sec', 2) * 5)
                    else:
                        if self.debug:
                            self._emit_log('this pokemon is not good enough to snipe {}'.format(pokemon))
                return WorkerResult.SUCCESS
            else:
                return self.snipe(pokemon)

        # check for pokeballs (excluding masterball)
        # checking again as we may have lost some if we sniped
        pokeballs_quantity = inventory.items().get(POKEBALL_ID).count
        superballs_quantity = inventory.items().get(GREATBALL_ID).count
        ultraballs_quantity = inventory.items().get(ULTRABALL_ID).count

        if pokeballs_quantity + superballs_quantity + ultraballs_quantity < self.min_ball:
            return WorkerResult.SUCCESS

        nearest_fort = self.get_nearest_fort_on_the_way(pokemon)

        if pokemon['is_vip'] or nearest_fort is None:
            # lock catching(with pokemon_id specified) while moving to vip pokemon or no fort around
            self.bot.capture_locked = pokemon['pokemon_id']
            step_walker = self._move_to(pokemon)
            if not step_walker.step():

                if pokemon['dist'] < Constants.MAX_DISTANCE_POKEMON_IS_REACHABLE:
                    self._encountered(pokemon)
                    self.bot.capture_locked = False  # unlock catch_worker
                    self.inspect(pokemon)
                    return WorkerResult.SUCCESS
                else:
                    return WorkerResult.RUNNING

        else:
            step_walker = self._move_to_pokemon_througt_fort(nearest_fort, pokemon)
            if not step_walker or not step_walker.step():
                return WorkerResult.RUNNING

    def _emit_failure(self, msg):
        self.emit_event(
            'move_to_map_pokemon_fail',
            formatted='Failure! {message}',
            data={'message': msg}
        )

    def _emit_log(self, msg):
        self.emit_event(
            'move_to_map_pokemon',
            formatted='{message}',
            data={'message': msg}
        )

    def _pokemon_event_data(self, pokemon):
        """Generates parameters used for the Bot's event manager.

        Args:
            pokemon: Pokemon object

        Returns:
            Dictionary with Pokemon's info.
        """
        now = int(time.time())
        return {
            'poke_name': pokemon['name'],
            'poke_dist': (format_dist(pokemon['dist'], self.unit)),
            'poke_lat': pokemon['latitude'],
            'poke_lon': pokemon['longitude'],
            'disappears_in': (format_time(pokemon['disappear_time'] - now))
        }

    def _teleport_to(self, pokemon):
        self.emit_event(
            'move_to_map_pokemon_teleport_to',
            formatted='Teleporting to {poke_name}. ({poke_dist})',
            data=self._pokemon_event_data(pokemon)
        )
        self.bot.api.set_position(pokemon['latitude'], pokemon['longitude'], self.alt, True)

    def _encountered(self, pokemon):
        self.emit_event(
            'move_to_map_pokemon_encounter',
            formatted='Encountered Pokemon: {poke_name}',
            data=self._pokemon_event_data(pokemon)
        )

    def _teleport_back(self, last_position):
        self.emit_event(
            'move_to_map_pokemon_teleport_back',
            formatted='Teleporting back to previous location ({last_lat}, {last_lon})...',
            data={'last_lat': last_position[0], 'last_lon': last_position[1]}
        )

    def _move_to(self, pokemon):
        """Moves trainer towards a Pokemon.

        Args:
            pokemon: Pokemon to move to.

        Returns:
            Walker
        """
        self.emit_event(
            'move_to_map_pokemon_move_towards',
            formatted=('Moving towards {poke_name}, {poke_dist}, left ('
                       '{disappears_in})'),
            data=self._pokemon_event_data(pokemon)
        )
        return walker_factory(self.walker, self.bot, pokemon['latitude'], pokemon['longitude'])

    def _move_to_pokemon_througt_fort(self, fort, pokemon):
        """Moves trainer towards a fort before a Pokemon.

        Args:
            fort

        Returns:
            StepWalker
        """

        nearest_fort = fort

        lat = nearest_fort['latitude']
        lng = nearest_fort['longitude']
        fortID = nearest_fort['id']
        details = fort_details(self.bot, fortID, lat, lng)
        fort_name = details.get('name', 'Unknown')

        unit = self.bot.config.distance_unit  # Unit to use when printing formatted distance

        dist = distance(
            self.bot.position[0],
            self.bot.position[1],
            lat,
            lng
        )

        if dist > Constants.MAX_DISTANCE_FORT_IS_REACHABLE:
            pokemon_throught_fort_event_data = {
                'fort_name': u"{}".format(fort_name),
                'distance': format_dist(dist, unit),
                'poke_name': pokemon['name'],
                'poke_dist': (format_dist(pokemon['dist'], self.unit))
            }

            self.emit_event(
                'moving_to_pokemon_throught_fort',
                formatted="Moving towards {poke_name} - {poke_dist}  through pokestop  {fort_name} - {distance}",
                data=pokemon_throught_fort_event_data
            )
        else:
            self.emit_event(
                'arrived_at_fort',
                formatted='Arrived at fort.'
            )

        return walker_factory(self.walker, self.bot, lat, lng)

    def get_nearest_fort_on_the_way(self, pokemon):
        forts = self.bot.get_forts(order_by_distance=True)

        # Remove stops that are still on timeout
        forts = filter(lambda x: x["id"] not in self.bot.fort_timeouts, forts)
        i = 0
        while i < len(forts):
            ratio = float(self.config.get('max_extra_dist_fort', 20))
            dist_self_to_fort = distance(self.bot.position[0], self.bot.position[1], forts[i]['latitude'],
                                         forts[i]['longitude'])
            dist_fort_to_pokemon = distance(pokemon['latitude'], pokemon['longitude'], forts[i]['latitude'],
                                            forts[i]['longitude'])
            total_dist = dist_self_to_fort + dist_fort_to_pokemon
            dist_self_to_pokemon = distance(self.bot.position[0], self.bot.position[1], pokemon['latitude'],
                                            pokemon['longitude'])
            if total_dist < (1 + (ratio / 100)) * dist_self_to_pokemon:
                i += 1
            else:
                del forts[i]
            # Return nearest fort if there are remaining
        if len(forts):
            return forts[0]
        else:
            return None
