#!/usr/bin/env python3

import re
import sys
import json
import requests
import os


from bs4 import BeautifulSoup
from bs4 import HT
from dataclasses import dataclass

# Type Hints
from typing import List, Sequence, Dict

# write machinery to find `eventID` according to the CTF
URL = 'https://ctftime.org/event/{EVENT_ID}/tasks/'
HEADERS = {
    'User-Agent':'Mozilla/5.0'
}


# ----- [ Class Event ] ----- # BEGIN #
class Event():
    '''
    Event class
        name : str        
        id : int
        ctf_id : int
        ctftime_url : str
        e_url : str
        e_title : str
    '''

    s = requests.Session()

    def __init__(self, 
                e_name : str = '',
                e_id : int = 0,
                e_ctf_id : int = 0,
                e_ctftime_url : str = '',
                e_url : str = '',
                e_title : str = ''
                ):
        self.e_name = e_name

        ''' @raises ValueError '''
        self.e_id = int( e_id )
        self.e_ctf_id = int( e_ctf_id )
            
        self.e_ctftime_url = e_ctftime_url
        self.e_url = e_url
        self.e_title = e_title
        self.writeups = self.find_event_writeups

    
    def __dict__(self) -> Dict:
        e_dict = dict ({
            "name" : self.e_name,
             "id" : str(self.e_id),
             "ctf_id" : str(self.e_ctf_id),
             "ctftime_url" : str(self.e_ctftime_url),
             "url" : str(self.e_url),
             "title" : str(self.e_title)
        })
        return e_dict

    def find_event_by_name(self):
        '''
        Given @name finds @event_id and fills needed fields to be used by @find_event_by_id
        Raises @ValueError
        '''
        
        url = 'https://ctftime.org/event/list/past'
        r = (self.s).get(url=url, headers=HEADERS)
        soup = BeautifulSoup(r.text, 'html.parser')

        # find Event by name
        found = False
        try:
            all_tr = soup.body.table.find_all('tr')[1::]
        except AttributeError:
            print("Error: Unable to get list")

        for i in all_tr:
            name = (i.td).text
            e_name = self.e_name.split('.')[0]
            if e_name.upper() in name.upper():
                found = True
                href = (i.a).get('href')
                start_ix, end_ix = re.search(r'\d+', href).span()
                self.e_id = int( href[start_ix:end_ix] )
                self.e_name = name
                break
        if not found:
            raise ValueError('Event not found')
    
    def find_event_by_id(self):
        '''
        Using the @event_id finds and collects available writeups into a list
        '''
        
        url = f'https://ctftime.org/event/{ str(self.e_id) }/tasks/'
        r = (self.s).get(url=url, headers=HEADERS)
        soup = BeautifulSoup(r.text, 'html.parser')

        writeups = []
        all_tr = soup.find_all('tr')
        for i in all_tr[1:]:
            writeup = Writeup()
            writeup.url = (i.a).get("href")
            writeup.name = (i.a).get_text()

            try:
                writeup.tags = list(map(lambda x: x.get_text(), i.find_all('span')))
                all_td = i.find_all('td')
                if len(all_td) > 1:
                    writeup.points = all_td[1].get_text()
                    writeup.no_writeups = int(all_td[-2].get_text())
            except ValueError:
                pass
            except IndexError:
                pass

            writeups.append( writeup )

        self.writeups = writeups
        return self.e_name, self.writeups

    def find_event_writeups(self):
        self.find_event_by_name()
        return self.find_event_by_id()

@dataclass
class Writeup():
    '''
    Writeup Class
        name : str
        points : int
        tags : list
        no_writeups : int
        url : str
    '''
    name : str = ''
    points : int = 0
    tags : Sequence[str] = None
    no_writeups : int = 0
    url : str = ''

    def __str__(self) -> str:
        '''
        String representation of class Writeup
        '''
        return f'Name: {self.name} ({self.points} pts)\n' +\
               f'Tags: {self.tags}\n' +\
               f'#Writeups: {self.no_writeups}\n' +\
               f'Writeup URL: https://ctftime.org{self.url}\n'

if __name__ == '__main__':
    
    if len(sys.argv) == 2:
        e_name = str( sys.argv[1] )
        event = Event (
            e_name = e_name
        )
        try:
            ctf_name, writeups = event.find_event_writeups()
        except ValueError as e:
            print( 'Could not find such event' )
            sys.exit( 1 )

        n = len(ctf_name)
        print('=' * n, ctf_name, '=' * n, sep='\n')
        print( '\n'.join( map( str, writeups )))
        sys.exit(0)
    else:
        print(f'Usage: {sys.argv[0]} <ctf-name>')
        sys.exit(1)
