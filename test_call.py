{\rtf1\ansi\ansicpg1252\cocoartf2822
\cocoatextscaling0\cocoaplatform0{\fonttbl\f0\fswiss\fcharset0 Helvetica;\f1\fnil\fcharset0 HelveticaNeue;}
{\colortbl;\red255\green255\blue255;}
{\*\expandedcolortbl;;}
\margl1440\margr1440\vieww11520\viewh8400\viewkind0
\pard\tx720\tx1440\tx2160\tx2880\tx3600\tx4320\tx5040\tx5760\tx6480\tx7200\tx7920\tx8640\pardirnatural\partightenfactor0

\f0\fs24 \cf0 from twilio.rest import Client\
\
# Use standard straight quotes\
account_sid = 'AC00f63d1401a9cd0fcf87800a0f5dc416' \
auth_token = '
\f1\fs26 b04c57abd5b695a51ba6a5ec3a90be33
\f0\fs24 ' # Not the API Secret, the main Auth Token\
client = Client(account_sid, auth_token)\
\
try:\
    call = client.calls.create(\
        to='+19176799405',  \
        from_='+18334329646',\
        url='https://handler.twilio.com/twiml/EH5d2f909a60a6cd5ca7c007d591ad9f5b'\
    )\
    print(f"Call initiated successfully! SID: \{call.sid\}")\
except Exception as e:\
    print(f"Failed to call: \{e\}")}