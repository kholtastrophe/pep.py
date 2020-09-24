import re
import sys
import time
import traceback

from datetime import datetime

from common.constants import privileges
from common.log import logUtils as log
from common.ripple import userUtils
from constants import exceptions
from constants import serverPackets
from helpers import aobaHelper
from helpers import chatHelper as chat
from helpers import countryHelper
from helpers import locationHelper
from helpers import kotrikhelper
from objects import glob
from ainu import utils as sim # too bad, this is ainu privated :older_man:

curryear = int(datetime.now().year)
today = datetime.date(datetime(curryear, int(datetime.now().month), int(datetime.now().day)))
peppyday = datetime.date(datetime(curryear, 4, 20))

def handle(tornadoRequest):
	# Data to return
	responseToken = None
	responseTokenString = "ayy"
	responseData = bytes()

	# Get IP from tornado request
	requestIP = tornadoRequest.getRequestIP()

	# Avoid exceptions
	clientData = ["unknown", "unknown", "unknown", "unknown", "unknown"]
	osuVersion = "unknown"

	# Split POST body so we can get username/password/hardware data
	# 2:-3 thing is because requestData has some escape stuff that we don't need
	loginData = str(tornadoRequest.request.body)[2:-3].split("\\n")
	try:
		# Make sure loginData is valid
		if len(loginData) < 3:
			raise exceptions.invalidArgumentsException()

		# Get HWID, MAC address and more
		# Structure (new line = "|", already split)
		# [0] osu! version
		# [1] plain mac addressed, separated by "."
		# [2] mac addresses hash set
		# [3] unique ID
		# [4] disk ID
		splitData = loginData[2].split("|")
		osuVersion = splitData[0] # osu! version
		timeOffset = int(splitData[1]) # timezone
		showCity = int(splitData[2]) # allow to show city
		clientData = splitData[3].split(":")[:5] # security hash
		blockNonFriendPM = int(splitData[4]) # allow PM
		if len(clientData) < 4:
			raise exceptions.forceUpdateException()

		# Try to get the ID from username
		username = str(loginData[0])
		userID = userUtils.getID(username)

		if not userID:
			# Invalid username
			raise exceptions.loginFailedException()
		if not userUtils.checkLogin(userID, loginData[1]):
			# Invalid password
			raise exceptions.loginFailedException()

		# Make sure we are not banned or locked
		priv = userUtils.getPrivileges(userID)
		if userUtils.isBanned(userID) and priv & privileges.USER_PENDING_VERIFICATION == 0:
			raise exceptions.loginBannedException()
		if userUtils.isLocked(userID) and priv & privileges.USER_PENDING_VERIFICATION == 0:
			raise exceptions.loginLockedException()

		# 2FA check
		if userUtils.check2FA(userID, requestIP):
			log.warning("Need 2FA check for user {}".format(loginData[0]))
			raise exceptions.need2FAException()

		# No login errors!

		# Verify this user (if pending activation)
		firstLogin = False
		if priv & privileges.USER_PENDING_VERIFICATION > 0 or not userUtils.hasVerifiedHardware(userID):
			if userUtils.verifyUser(userID, clientData):
				# Valid account
				log.info("Account ID {} verified successfully!".format(userID))
				glob.verifiedCache[str(userID)] = 1
				firstLogin = True
			else:
				# Multiaccount detected
				log.info("Account ID {} NOT verified!".format(userID))
				glob.verifiedCache[str(userID)] = 0
				raise exceptions.loginBannedException()


		# Save HWID in db for multiaccount detection
		hwAllowed = userUtils.logHardware(userID, clientData, firstLogin)

		# This is false only if HWID is empty
		# if HWID is banned, we get restricted so there's no
		# need to deny bancho access
		if not hwAllowed:
			raise exceptions.haxException()

		# Log user IP
		userUtils.logIP(userID, requestIP)

		# Log user osuver
		kotrikhelper.setUserLastOsuVer(userID, osuVersion)

		# Delete old tokens for that user and generate a new one
		isTournament = "tourney" in osuVersion
		numericVersion = re.sub(r'[^0-9.]', '', osuVersion)
		if not isTournament:
			glob.tokens.deleteOldTokens(userID)
		if numericVersion < glob.conf.config["server"]["osuminver"]:
			raise exceptions.forceUpdateException()

		responseToken = glob.tokens.addToken(userID, requestIP, timeOffset=timeOffset, tournament=isTournament)
		responseTokenString = responseToken.token

		# Check restricted mode (and eventually send message)
		responseToken.checkRestricted()

		# Send message if donor expires soon
		if responseToken.privileges & privileges.USER_DONOR > 0:
			expireDate = userUtils.getDonorExpire(responseToken.userID)
			if expireDate-int(time.time()) <= 86400*3:
				expireDays = round((expireDate-int(time.time()))/86400)
				expireIn = "{} days".format(expireDays) if expireDays > 1 else "less than 24 hours"
				responseToken.enqueue(serverPackets.notification("Your donor tag expires in {}! When your donor tag expires, you won't have any of the donor privileges, like yellow username, custom badge and other good stuff! If you wish to keep supporting Ainu and you don't want to lose your donor privileges, you can donate again by clicking on the 'heart' icon on Ainu's website.".format(expireIn)))

		# Set silence end UNIX time in token
		responseToken.silenceEndTime = userUtils.getSilenceEnd(userID)

		# Get only silence remaining seconds
		silenceSeconds = responseToken.getSilenceSecondsLeft()

		# Get supporter/GMT
		userGMT = False
		if not userUtils.isRestricted(userID):
			userSupporter = True
		else:
			userSupporter = False
		userTournament = False
		if responseToken.admin:
			userGMT = True
		if responseToken.privileges & privileges.USER_TOURNAMENT_STAFF > 0:
			userTournament = True

		# Server restarting check
		if glob.restarting:
			raise exceptions.banchoRestartingException()

		if sim.checkIfFlagged(userID):
			responseToken.enqueue(serverPackets.notification("Staff suspect you of cheat! You have 5 days to make a full pc startup liveplay, or you will get restricted and you'll have to wait a month to appeal!"))

		# Check If today is 4/20 (Peppy Day)
		if today == peppyday:
			if glob.conf.extra["mode"]["peppyday"]:
				responseToken.enqueue(serverPackets.notification("Everyone on today will have peppy as their profile picture! Have fun on peppy day"))

		# Send login notification before maintenance message
		if glob.banchoConf.config["loginNotification"] != "":
			responseToken.enqueue(serverPackets.notification(glob.banchoConf.config["loginNotification"]))

		# Maintenance check
		if glob.banchoConf.config["banchoMaintenance"]:
			if not userGMT:
				# We are not mod/admin, delete token, send notification and logout
				glob.tokens.deleteToken(responseTokenString)
				raise exceptions.banchoMaintenanceException()
			else:
				# We are mod/admin, send warning notification and continue
				responseToken.enqueue(serverPackets.notification("Bancho is in maintenance mode. Only mods/admins have full access to the server.\nType !system maintenance off in chat to turn off maintenance mode."))

		# BAN CUSTOM CHEAT CLIENTS
		# 0Ainu = First Ainu build
		# b20190326.2 = Ainu build 2 (MPGH PAGE 10)
		# b20190401.22f56c084ba339eefd9c7ca4335e246f80 = Ainu Aoba's Birthday Build
		# b20190906.1 = Unknown Ainu build? (unreleased, I think)
		# b20191223.3 = Unknown Ainu build? (Taken from most users osuver in cookiezi.pw)
		# b20190226.2 = hqOsu (hq-af)
		if glob.conf.extra["mode"]["anticheat"]:
			# Ainu Client 2020 update
			if tornadoRequest.request.headers.get("ainu") == "happy":
				log.info("Account ID {} tried to use Ainu (Cheat) Client 2020!".format(userID))
				if userUtils.isRestricted(userID):
					responseToken.enqueue(serverPackets.notification("You're banned because you're currently using Ainu Client... Happy New Year 2020 and Enjoy your restriction :)"))
					#if glob.conf.config["discord"]["enable"] == True:
					webhook = aobaHelper.Webhook(glob.conf.config["discord"]["anticheat"],color=0xadd8e6,footer="Man... this is worst player. [ Login Gate AC ]")
					webhook.set_title(title="Catched some cheater Account ID {}".format(userID))
					webhook.set_desc("{} tried to use Ainu (Cheat) Client 2020! AGAIN!!!".format(username))
					log.info("Sent to webhook {} DONE!!".format(glob.conf.config["discord"]["enable"]))
					aobaHelper.Webhook.post()
				else:
					glob.tokens.deleteToken(userID)
					userUtils.restrict(userID)
					#if glob.conf.config["discord"]["enable"] == True:
					webhook = aobaHelper.Webhook(glob.conf.config["discord"]["anticheat"],color=0xadd8e6,footer="Man... this is worst player. [ Login Gate AC ]")
					webhook.set_title(title="Catched some cheater Account ID {}".format(userID))
					webhook.set_desc("{} tried to use Ainu (Cheat) Client 2020 and got restricted!".format(username))
					log.info("Sent to webhook {} DONE!!".format(glob.conf.config["discord"]["enable"]))
					webhook.post()
					raise exceptions.loginCheatClientsException()

			elif tornadoRequest.request.headers.get("a") == "@_@_@_@_@_@_@_@___@_@_@_@___@_@___@":
				log.info("Account ID {} tried to use secret!".format(userID))
				if userUtils.isRestricted(userID):
					responseToken.enqueue(serverPackets.notification("You're banned because you're currently using some darkness secret that no one has..."))
					#if glob.conf.config["discord"]["enable"] == True:
					webhook = aobaHelper.Webhook(glob.conf.config["discord"]["anticheat"],color=0xadd8e6,footer="@_@_@_@_@_@_@_@___@_@_@_@___@_@___@")
					webhook.set_title(title="Catched some cheater Account ID {}".format(userID))
					webhook.set_desc("{} tried to use secret... again.".format(username))
					log.info("Sent to webhook {} DONE!!".format(glob.conf.config["discord"]["enable"]))
					aobaHelper.Webhook.post()
				else:
					glob.tokens.deleteToken(userID)
					userUtils.restrict(userID)
					#if glob.conf.config["discord"]["enable"] == True:
					webhook = aobaHelper.Webhook(glob.conf.config["discord"]["anticheat"],color=0xadd8e6,footer="@_@_@_@_@_@_@_@___@_@_@_@___@_@___@")
					webhook.set_title(title="Catched some cheater Account ID {}".format(userID))
					webhook.set_desc("{} tried to @_@_@_@_@_@_@_@___@_@_@_@___@_@___@ and got restricted!".format(username))
					log.info("Sent to webhook {} DONE!!".format(glob.conf.config["discord"]["enable"]))
					webhook.post()
					raise exceptions.loginCheatClientsException()

			# Ainu Client 2019
			elif aobaHelper.getOsuVer(userID) in ["0Ainu", "b20190326.2", "b20190401.22f56c084ba339eefd9c7ca4335e246f80", "b20190906.1", "b20191223.3"]:
				log.info("Account ID {} tried to use Ainu (Cheat) Client!".format(userID))
				if userUtils.isRestricted(userID):
					responseToken.enqueue(serverPackets.notification("You're banned because you're currently using Ainu Client. Enjoy your restriction :)"))
					#if glob.conf.config["discord"]["enable"] == True:
					webhook = aobaHelper.Webhook(glob.conf.config["discord"]["anticheat"],color=0xadd8e6,footer="Man... this is worst player. [ Login Gate AC ]")
					webhook.set_title(title="Catched some cheater Account ID {}".format(userID))
					webhook.set_desc("{} tried to use Ainu (Cheat) Client! AGAIN!!!".format(username))
					log.info("Sent to webhook {} DONE!!".format(glob.conf.config["discord"]["enable"]))
					webhook.post()
				else:
					glob.tokens.deleteToken(userID)
					userUtils.restrict(userID)
					#if glob.conf.config["discord"]["enable"] == True:
					webhook = aobaHelper.Webhook(glob.conf.config["discord"]["anticheat"],color=0xadd8e6,footer="Man... this is worst player. [ Login Gate AC ]")
					webhook.set_title(title="Catched some cheater Account ID {}".format(userID))
					webhook.set_desc("{} tried to use Ainu (Cheat) Client and got restricted!".format(username))
					log.info("Sent to webhook {} DONE!!".format(glob.conf.config["discord"]["enable"]))
					webhook.post()
					raise exceptions.loginCheatClientsException()

			# hqOsu
			elif aobaHelper.getOsuVer(userID) == "b20190226.2":
				log.info("Account ID {} tried to use hqOsu!".format(userID))
				if userUtils.isRestricted(userID):
					responseToken.enqueue(serverPackets.notification("Trying to use hqOsu in here? Well... No, sorry. We don't allow cheats here. Go play https://cookiezi.pw or others cheat server."))
					#if glob.conf.config["discord"]["enable"] == True:
					webhook = aobaHelper.Webhook(glob.conf.config["discord"]["anticheat"],color=0xadd8e6,footer="Man... this is worst player. [ Login Gate AC ]")
					webhook.set_title(title="Catched some cheater Account ID {}".format(userID))
					webhook.set_desc("{} tried to use hqOsu! AGAIN!!!".format(username))
					log.info("Sent to webhook {} DONE!!".format(glob.conf.config["discord"]["enable"]))
					webhook.post()
				else:
					glob.tokens.deleteToken(userID)
					userUtils.restrict(userID)
					#if glob.conf.config["discord"]["enable"] == True:
					webhook = aobaHelper.Webhook(glob.conf.config["discord"]["anticheat"],color=0xadd8e6,footer="Man... this is worst player. [ Login Gate AC ]")
					webhook.set_title(title="Catched some cheater Account ID {}".format(userID))
					webhook.set_desc("{} tried to use hqOsu and got restricted!".format(username))
					log.info("Sent to webhook {} DONE!!".format(glob.conf.config["discord"]["enable"]))
					webhook.post()
					raise exceptions.loginCheatClientsException()

		# Send all needed login packets
		responseToken.enqueue(serverPackets.silenceEndTime(silenceSeconds))
		responseToken.enqueue(serverPackets.userID(userID))
		responseToken.enqueue(serverPackets.protocolVersion())
		responseToken.enqueue(serverPackets.userSupporterGMT(userSupporter, userGMT, userTournament))
		responseToken.enqueue(serverPackets.userPanel(userID, True))
		responseToken.enqueue(serverPackets.userStats(userID, True))

		# Channel info end (before starting!?! wtf bancho?)
		responseToken.enqueue(serverPackets.channelInfoEnd())
  
		# set user to online
		sim.setUserOnline(userID, 1)
		# Default opened channels
		# TODO: Configurable default channels
		chat.joinChannel(token=responseToken, channel="#osu")
		chat.joinChannel(token=responseToken, channel="#announce")

		# Join admin channel if we are an admin
		if responseToken.admin:
			chat.joinChannel(token=responseToken, channel="#admin")

		# Output channels info
		for key, value in glob.channels.channels.items():
			if value.publicRead and not value.hidden:
				responseToken.enqueue(serverPackets.channelInfo(key))

		# Send friends list
		responseToken.enqueue(serverPackets.friendList(userID))

		# Send main menu icon
		if glob.banchoConf.config["menuIcon"] != "":
			responseToken.enqueue(serverPackets.mainMenuIcon(glob.banchoConf.config["menuIcon"]))

		# Send online users' panels
		with glob.tokens:
			for _, token in glob.tokens.tokens.items():
				if not token.restricted:
					responseToken.enqueue(serverPackets.userPanel(token.userID))

		# Get location and country from ip.zxq.co or database
		if glob.localize:
			# Get location and country from IP
			latitude, longitude = locationHelper.getLocation(requestIP)
			if userID == 1000:
				latitude, longitude = 34.676143, 133.938883
			countryLetters = locationHelper.getCountry(requestIP)
			country = countryHelper.getCountryID(countryLetters)
		else:
			# Set location to 0,0 and get country from db
			log.warning("Location skipped")
			latitude = 0
			longitude = 0
			countryLetters = "XX"
			country = countryHelper.getCountryID(userUtils.getCountry(userID))

		# Set location and country
		responseToken.setLocation(latitude, longitude)
		responseToken.country = country

		# Set country in db if user has no country (first bancho login)
		if userUtils.getCountry(userID) == "XX":
			userUtils.setCountry(userID, countryLetters)

		# Send to everyone our userpanel if we are not restricted or tournament
		if not responseToken.restricted:
			glob.streams.broadcast("main", serverPackets.userPanel(userID))

		# Set reponse data to right value and reset our queue
		responseData = responseToken.queue
		responseToken.resetQueue()
	except exceptions.loginFailedException:
		# Login failed error packet
		# (we don't use enqueue because we don't have a token since login has failed)
		responseData += serverPackets.loginFailed()
	except exceptions.invalidArgumentsException:
		# Invalid POST data
		# (we don't use enqueue because we don't have a token since login has failed)
		responseData += serverPackets.loginFailed()
		responseData += serverPackets.notification("I see what you're doing...")
	except exceptions.loginBannedException:
		# Login banned error packet
		responseData += serverPackets.loginBanned()
	except exceptions.loginLockedException:
		# Login banned error packet
		responseData += serverPackets.loginLocked()
	except exceptions.loginCheatClientsException:
		# Banned for logging in with cheats
		responseData += serverPackets.loginCheats()
	except exceptions.banchoMaintenanceException:
		# Bancho is in maintenance mode
		responseData = bytes()
		if responseToken is not None:
			responseData = responseToken.queue
		responseData += serverPackets.notification("Our bancho server is in maintenance mode. Please try to login again later.")
		responseData += serverPackets.loginFailed()
	except exceptions.banchoRestartingException:
		# Bancho is restarting
		responseData += serverPackets.notification("Bancho is restarting. Try again in a few minutes.")
		responseData += serverPackets.loginFailed()
	except exceptions.need2FAException:
		# User tried to log in from unknown IP
		responseData += serverPackets.needVerification()
	except exceptions.haxException:
		# Uh...
		responseData += serverPackets.notification("Your HWID is banned.")
		responseData += serverPackets.loginFailed()
	except exceptions.forceUpdateException:
		# This happens when you:
		# - Using older build than config set
		# - Using oldoldold client, we don't have client data. Force update.
		# (we don't use enqueue because we don't have a token since login has failed)
		responseData += serverPackets.forceUpdate()
	except:
		log.error("Unknown error!\n```\n{}\n{}```".format(sys.exc_info(), traceback.format_exc()))
	finally:
		# Console and discord log
		if len(loginData) < 3:
			log.info("Invalid bancho login request from **{}** (insufficient POST data)".format(requestIP), "bunker")

		# Return token string and data
		return responseTokenString, responseData
