import gspread
from google.oauth2.service_account import Credentials
from typing import List, Dict

class GoogleSheetsManager:
    def __init__(self, credentials_file: str, sheet_id: str):
        scope = ["https://www.googleapis.com/auth/spreadsheets",
                 "https://www.googleapis.com/auth/drive.file",
                 "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_file(credentials_file, scopes=scope)
        self.client = gspread.authorize(creds)
        self.sheet = self.client.open_by_key(sheet_id)
    
    def get_worksheet(self, title: str):
        return self.sheet.worksheet(title)
    
    def load_teams(self) -> Dict:
        ws = self.get_worksheet("teams")
        teams = {}
        for row in ws.get_all_records():
            teams[row['id']] = {
                'team_short': row.get('team_short', 'ERR'),
                'division': row['division'],
                'conference': row['conference'],
                'gm_id': row['gm_id'],
                'name': row['name']
            }
        return teams
    
    def load_users(self) -> Dict:
        ws = self.get_worksheet("users")
        users = {}
        for row in ws.get_all_records():
            users[row['id']] = {
                'username': row['username'],
                'screen_name': row['screen_name'],
                'timezone': row['timezone'],
                'team_pick_order': row['team_pick_order']
            }
        return users
    
    def load_prospects(self) -> Dict:
        ws = self.get_worksheet("prospects")
        prospects = {}
        for row in ws.get_all_records():
            prospects[row['id']] = {
                'f_name': row['f_name'],
                'l_name': row['l_name'],
                'college': row['college'],
                'position_id': row['position_id'],
                'ranking': row['ranking'],
                'drafted': row['drafted'] == 'TRUE'
            }
        return prospects
    
    def load_picks(self) -> List:
        ws = self.get_worksheet("picks")
        picks = []
        for row in ws.get_all_records():
            picks.append({
                'id': row['id'],
                'team_id': row['team_id'],
                'otc_at': row.get('otc_at'),
                'player_id': row.get('player_id'),
                'picked_at': row.get('picked_at'),
                'clock_expire': row.get('clock_expire') == 'TRUE'
            })
        return sorted(picks, key=lambda x: x['id'])
    
    def load_positions(self) -> Dict:
        ws = self.get_worksheet("positions")
        positions = {}
        for row in ws.get_all_records():
            positions[row['id']] = row['position']
        return positions
    
    def update_prospect_drafted(self, prospect_id: int, status: bool = True):
        ws = self.get_worksheet("prospects")
        cell = ws.find(str(prospect_id), in_column=1)
        ws.update_cell(cell.row, 7, "TRUE" if status else "FALSE")
    
    def update_pick(self, pick_id: int, player_id: int, picked_at: str):
        ws = self.get_worksheet("picks")
        cell = ws.find(str(pick_id), in_column=1)
        ws.update_cell(cell.row, 4, player_id)
        ws.update_cell(cell.row, 5, picked_at)