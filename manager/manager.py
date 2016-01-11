#!/usr/bin/env python

import datetime
import hashlib
import importlib
import json
import logging
import logging.config
import os
import pika
import sys
import threading
import time

from tools import config
from tools import utils
from tools.db import database as db


class Manager:

	def __init__(self):
		try: #TODO: this should be nicer...		
			logging.config.fileConfig(os.path.join(PROJECT_PATH, 'logging.conf'), defaults={'logfilename': 'manager.log'})
		except Exception, e:
			print "Error while trying to load config file for logging"

		try:
			config.load("manager")
		except ValueError: # Config file can't be loaded, e.g. no valid JSON
			logging.exception("Wasn't able to load config file, exiting...")
			quit()
		
		try:
			db.connect(PROJECT_PATH)
		except:
			logging.exception("Couldn't connect to database!")
			quit()
		
		self.notifiers = []
		self.received_data_counter = 0
		self.current_alarm_dir = "/var/tmp/secpi/alarms"
		self.data_timeout = 10
		self.num_of_workers = 0
		self.mail_enabled = False
		self.holddown_state = False
		self.holddown_timer = 30

		credentials = pika.PlainCredentials(config.get('rabbitmq')['user'], config.get('rabbitmq')['password'])
		parameters = pika.ConnectionParameters(credentials=credentials,
			host=config.get('rabbitmq')['master_ip'],
			port=5671,
			ssl=True,
			socket_timeout=10,
			ssl_options = {
				"ca_certs":PROJECT_PATH+"/certs/"+config.get('rabbitmq')['cacert'],
				"certfile":PROJECT_PATH+"/certs/"+config.get('rabbitmq')['certfile'],
				"keyfile":PROJECT_PATH+"/certs/"+config.get('rabbitmq')['keyfile']
			}
		)
		# TODO: add exception
		self.connection = pika.BlockingConnection(parameters=parameters)
		self.channel = self.connection.channel()

		#define exchange
		self.channel.exchange_declare(exchange=utils.EXCHANGE, exchange_type='direct')

		#define queues: data, alarm and action & config for every pi
		self.channel.queue_declare(queue=utils.QUEUE_DATA)
		self.channel.queue_declare(queue=utils.QUEUE_ALARM)
		self.channel.queue_declare(queue=utils.QUEUE_ON_OFF)
		self.channel.queue_declare(queue=utils.QUEUE_LOG)
		self.channel.queue_declare(queue=utils.QUEUE_INIT_CONFIG)
		self.channel.queue_bind(exchange=utils.EXCHANGE, queue=utils.QUEUE_ON_OFF)
		self.channel.queue_bind(exchange=utils.EXCHANGE, queue=utils.QUEUE_DATA)
		self.channel.queue_bind(exchange=utils.EXCHANGE, queue=utils.QUEUE_ALARM)
		self.channel.queue_bind(exchange=utils.EXCHANGE, queue=utils.QUEUE_LOG)
		self.channel.queue_bind(exchange=utils.EXCHANGE, queue=utils.QUEUE_INIT_CONFIG)
		
		# load workers from db
		workers = db.session.query(db.objects.Worker).all()
		for pi in workers:
			self.channel.queue_declare(queue=utils.QUEUE_ACTION+str(pi.id))
			self.channel.queue_declare(queue=utils.QUEUE_CONFIG+str(pi.id))
			self.channel.queue_bind(exchange=utils.EXCHANGE, queue=utils.QUEUE_ACTION+str(pi.id))
			self.channel.queue_bind(exchange=utils.EXCHANGE, queue=utils.QUEUE_CONFIG+str(pi.id))

		# debug output, setups & state
		setups = db.session.query(db.objects.Setup).all()
		rebooted = False
		for setup in setups:
			print "name: %s active:%s" % (setup.name, setup.active_state)
			if setup.active_state:
				rebooted = True

		if rebooted:
			self.setup_notifiers()

		#define callbacks for alarm and data queues
		self.channel.basic_consume(self.got_alarm, queue=utils.QUEUE_ALARM, no_ack=True)
		self.channel.basic_consume(self.got_on_off, queue=utils.QUEUE_ON_OFF, no_ack=True)
		self.channel.basic_consume(self.got_data, queue=utils.QUEUE_DATA, no_ack=True)
		self.channel.basic_consume(self.got_log, queue=utils.QUEUE_LOG, no_ack=True)
		self.channel.basic_consume(self.got_config_request, queue=utils.QUEUE_INIT_CONFIG, no_ack=True)
		logging.info("Setup done!")

	
	def start(self):
		self.channel.start_consuming()
		
	def __del__(self):
		try:
			self.connection.close()
		except AttributeError: #If there is no connection object closing won't work
			logging.info("No connection cleanup possible")

	
	# see: http://stackoverflow.com/questions/1176136/convert-string-to-python-class-object
	def class_for_name(self, module_name, class_name):
		try:
			# load the module, will raise ImportError if module cannot be loaded
			m = importlib.import_module(module_name)
			# get the class, will raise AttributeError if class cannot be found
			c = getattr(m, class_name)
			return c
		except ImportError as ie:
			self.log_err("Couldn't import module %s: %s"%(module_name, ie))
		except AttributeError as ae:
			self.log_err("Couldn't find class %s: %s"%(class_name, ae))
	

	# this method is used to send messages to a queue
	def send_message(self, rk, body, **kwargs):
		try:
			self.channel.basic_publish(exchange=utils.EXCHANGE, routing_key=rk, body=body, **kwargs)
			logging.info("Sending data to %s" % rk)
			return True
		except Exception as e:
			logging.exception("Error while sending data to queue:\n%s" % e)
			return False
	
	# this method is used to send json messages to a queue
	def send_json_message(self, rk, body, **kwargs):
		try:
			properties = pika.BasicProperties(content_type='application/json')
			self.channel.basic_publish(exchange=utils.EXCHANGE, routing_key=rk, body=json.dumps(body), properties=properties, **kwargs)
			logging.info("Sending json data to %s" % rk)
			return True
		except Exception as e:
			logging.exception("Error while sending json data to queue:\n%s" % e)
			return False
	
	# helper method to create error log entry
	def log_err(self, msg):
		logging.error(msg)
		log_entry = db.objects.LogEntry(level=utils.LEVEL_ERR, message=str(msg), sender="Manager")
		db.session.add(log_entry)
		db.session.commit()
	
	
	def got_config_request(self, ch, method, properties, body):
		ip_addresses = json.loads(body)
		logging.info("Got config request with following IP addresses: %s" % ip_addresses)

		pi_id = None
		worker = db.session.query(db.objects.Worker).filter(db.objects.Worker.address.in_(ip_addresses)).first()
		if worker:
			pi_id = worker.id
			logging.info("Found worker id %s for IP address %s" % (pi_id, worker.address))
		
		config = self.prepare_config(pi_id)
		logging.info("Sending intial config to worker with id %s" % pi_id)
		reply_properties = pika.BasicProperties(correlation_id=properties.correlation_id, content_type='application/json')
		self.channel.basic_publish(exchange=utils.EXCHANGE, properties=reply_properties, routing_key=properties.reply_to, body=json.dumps(config))

	# callback method for when the manager recieves data after a worker executed its actions
	def got_data(self, ch, method, properties, body): #TODO: error management
		logging.info("Got data")
		newFile_bytes = bytearray(body)
		if newFile_bytes: #only write data when body is not empty
			newFile = open("%s/%s.zip" % (self.current_alarm_dir, hashlib.md5(newFile_bytes).hexdigest()), "wb")
			newFile.write(newFile_bytes)
			logging.info("Data written")
		self.received_data_counter += 1

	# callback for log messages
	def got_log(self, ch, method, properties, body):
		log = json.loads(body)
		logging.debug("Got log message from %s: %s"%(log['sender'], log['msg']))
		log_entry = db.objects.LogEntry(level=log['level'], message=str(log['msg']), sender=log['sender'], logtime=utils.str_to_value(log['datetime']))
		db.session.add(log_entry)
		db.session.commit()

	# callback for when a setup gets activated/deactivated
	def got_on_off(self, ch, method, properties, body):
		msg = json.loads(body)
		
		# TODO: destructor for notifier?
		self.notifiers = []
		
		if(msg['active_state'] == True):
			self.setup_notifiers()
		
		logging.info("Activating PIs!")
		workers = db.session.query(db.objects.Worker).filter(db.objects.Worker.active_state == True).all()
		for pi in workers:
			config = self.prepare_config(pi.id)
			self.send_json_message(utils.QUEUE_CONFIG+str(pi.id), config)
			logging.info("Activated %s"%pi.name)

	# callback method which gets called when a worker raises an alarm
	def got_alarm(self, ch, method, properties, body):
		msg = json.loads(body)
		late_arrival = utils.check_late_arrival(datetime.datetime.strptime(msg["datetime"], "%Y-%m-%d %H:%M:%S"))

		if not late_arrival:
			logging.info("Received alarm: %s"%body)
		else:
			logging.info("Received old alarm: %s"%body)

		if not self.holddown_state:
			# put into holddown
			holddown_thread = threading.Thread(name="thread-holddown", target=self.holddown)
			holddown_thread.start()

			# TODO: adapt dir for current alarm
			self.current_alarm_dir = "/var/tmp/secpi/alarms/%s" % time.strftime("/%Y%m%d_%H%M%S")
			os.makedirs(self.current_alarm_dir) #TODO: exception handling
			logging.debug("Created directory for alarm: %s" % self.current_alarm_dir)
			self.received_data_counter = 0

			# interate over workers and send "execute"
			workers = db.session.query(db.objects.Worker).filter(db.objects.Worker.active_state == True).all()
			self.num_of_workers = len(workers)
			action_message = { "msg": "execute",
								"datetime": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
								"late_arrival":late_arrival}
			for pi in workers:
				self.send_json_message(utils.QUEUE_ACTION+str(pi.id), action_message)
			
			worker = db.session.query(db.objects.Worker).filter(db.objects.Worker.id == msg['pi_id']).first()
			sensor = db.session.query(db.objects.Sensor).filter(db.objects.Sensor.id == msg['sensor_id']).first()
			
			# create log entry for db
			if not late_arrival:
				al = db.objects.Alarm(sensor_id=msg['sensor_id'], message=msg['message'])
				lo = db.objects.LogEntry(level=utils.LEVEL_WARN, sender="Manager", message="New alarm from %s on sensor %s: %s"%( (worker.name if worker else msg['pi_id']) , (sensor.name if sensor else msg['sensor_id']) , msg['message']))
			else:
				al = db.objects.Alarm(sensor_id=msg['sensor_id'], message="Late Alarm: %s" %msg['message'])
				lo = db.objects.LogEntry(level=utils.LEVEL_WARN, sender="Manager", message="Old alarm from %s on sensor %s: %s"%( (worker.name if worker else msg['pi_id']) , (sensor.name if sensor else msg['sensor_id']) , msg['message']))
			db.session.add(al)
			db.session.add(lo)
			db.session.commit()
			
			# TODO: add information about late arrival of alarm
			notif_info = {
				"message": msg['message'],
				"sensor": (sensor.name if sensor else msg['sensor_id']),
				"sensor_id": msg['sensor_id'],
				"worker": (worker.name if worker else msg['pi_id']),
				"worker_id": msg['pi_id']
			}

			# start timeout thread for workers to reply
			timeout_thread = threading.Thread(name="thread-timeout", target=self.notify, args=[notif_info])
			timeout_thread.start()
		else: # --> holddown state
			logging.info("Received alarm but manager is in holddown state: %s" % body)
			al = db.objects.Alarm(sensor_id=msg['sensor_id'], message="Alarm during holddown state: %s" % msg['message'])
			lo = db.objects.LogEntry(level=utils.LEVEL_INFO, sender="Manager", message="Alarm during holddown state from %s on sensor %s: %s"%(msg['pi_id'], msg['sensor_id'], msg['message']))
			db.session.add(al)
			db.session.add(lo)
			db.session.commit()

	# initialize the notifiers
	def setup_notifiers(self):
		notifiers = db.session.query(db.objects.Notifier).filter(db.objects.Notifier.active_state == True).all()
		
		for notifier in notifiers:
			params = {}
			for p in notifier.params:
				params[p.key] = p.value
				
			n = self.class_for_name(notifier.module, notifier.cl)
			noti = n(notifier.id, params)
			self.notifiers.append(noti)
			logging.info("Set up notifier %s" % notifier.cl)

	# timeout thread which sends the received data from workers
	def notify(self, info):
		timeout = 30 # TODO: make this configurable
		for i in range(0, timeout):
			if self.received_data_counter < self.num_of_workers: #not all data here yet
				logging.debug("Waiting for data from workers: data counter: %d, #workers: %d" % (self.received_data_counter, self.num_of_workers))
				time.sleep(1)
			else:
				logging.debug("Received all data from workers, canceling the timeout")
				break
		# continue code execution
		if self.received_data_counter < self.num_of_workers:
			logging.info("TIMEOUT: Only %d out of %d workers replied with data" % (self.received_data_counter, self.num_of_workers))
			lo = db.objects.LogEntry(level=utils.LEVEL_INFO, sender="Manager", message="TIMEOUT: Only %d out of %d workers replied with data"%(self.received_data_counter, self.num_of_workers))
			db.session.add(lo)
			db.session.commit()
		
		try:
			for notifier in self.notifiers:
				notifier.notify(info)
		except Exception as ex:
			logging.exception("Error notifying: %s"%ex)

	def holddown(self):
		self.holddown_state = True
		for i in range(0, self.holddown_timer):
			time.sleep(1)
		logging.info("Holddown is over") #TODO: change to debug message
		self.holddown_state = False

	def prepare_config(self, pi_id):
		logging.info("Preparing config for worker with id %s" % pi_id)
		conf = {
			"pi_id": pi_id,
			"rabbitmq": config.get("rabbitmq"),
			"active": False, # default to false, will be overriden if should be true
		}
		
		sensors = db.session.query(db.objects.Sensor).join(db.objects.Zone).join((db.objects.Setup, db.objects.Zone.setups)).filter(db.objects.Setup.active_state == True).filter(db.objects.Sensor.worker_id == pi_id).all()
		
		# if we have sensors we are active
		if(len(sensors)>0):
			conf['active'] = True
		
		
		conf_sensors = []
		for sen in sensors:
			para = {}
			# create params array
			for p in sen.params:
				para[p.key] = p.value
			
			conf_sen = {
				"id": sen.id,
				"name": sen.name,
				"module": sen.module,
				"class": sen.cl,
				"params": para
			}
			conf_sensors.append(conf_sen)
		
		conf['sensors'] = conf_sensors
		
		actions = db.session.query(db.objects.Action).join((db.objects.Worker, db.objects.Action.workers)).filter(db.objects.Worker.id == pi_id).filter(db.objects.Action.active_state == True).all()
		# if we have actions we are also active
		if(len(actions)>0):
			conf['active'] = True
			
		conf_actions = []
		# iterate over all actions
		for act in actions:
			para = {}
			# create params array
			for p in act.params:
				para[p.key] = p.value
				
			conf_act = {
				"id": act.id,
				"module": act.module,
				"class": act.cl,
				"params": para
			}
			conf_actions.append(conf_act)
		
		conf['actions'] = conf_actions

		logging.info("Generated config: %s" % conf)
		return conf


if __name__ == '__main__':
	try:
		if(len(sys.argv)>1):
			PROJECT_PATH = sys.argv[1]
			mg = Manager()
			mg.start()
		else:
			print("Error initializing Manager, no path given!");
	except KeyboardInterrupt:
		logging.info("Shutting down manager!")
		# TODO: cleanup?
		try:
			sys.exit(0)
		except SystemExit:
			os._exit(0)
