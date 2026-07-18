# Converted from: Feature_engineer_zoom.ipynb

# ===== Cell 0 =====
# Cell 1: Import libraries
import pandas as pd
import numpy as np
import re

# ===== Cell 1 =====
# Cell 2: Load the combined data
df = pd.read_excel('All_Seasons_Combined.xlsx')

print(f"Loaded data: {len(df)} matches")
print(f"Columns: {df.columns.tolist()}")
df.head()

# ===== Cell 2 =====
# Cell 3: Extract goals from scores
def extract_goals(score_str):
    """Extract goals from score like '2-0' or '1-0HT'"""
    if pd.isna(score_str):
        return None, None
    
    # Remove 'HT' suffix
    score_str = str(score_str).replace('HT', '').strip()
    
    # Split by '-'
    parts = score_str.split('-')
    if len(parts) == 2:
        try:
            return int(parts[0]), int(parts[1])
        except:
            return None, None
    return None, None

# Extract FT goals
df[['Team A FT Goals', 'Team B FT Goals']] = df['FT Score'].apply(
    lambda x: pd.Series(extract_goals(x))
)

# Extract HT goals
df[['Team A HT Goals', 'Team B HT Goals']] = df['HT Score'].apply(
    lambda x: pd.Series(extract_goals(x))
)

print("✓ Goals extracted from scores")
df[['Team A', 'Team B', 'FT Score', 'Team A FT Goals', 'Team B FT Goals']].head()

# ===== Cell 3 =====
# Cell 4: Calculate basic features

# First, extract Round Number from Round column
def extract_round_number(round_text):
    """Extract number from 'Round 38' -> 38"""
    if pd.isna(round_text):
        return None
    match = re.search(r'(\d+)', str(round_text))
    if match:
        return int(match.group(1))
    return None

# Create Round Number column
df['Round Number'] = df['Round'].apply(extract_round_number)

# 1. FT Result (Target Variable)
def get_result(team_a_goals, team_b_goals):
    if pd.isna(team_a_goals) or pd.isna(team_b_goals):
        return None
    if team_a_goals > team_b_goals:
        return 'Home Win'  # Team A wins
    elif team_a_goals < team_b_goals:
        return 'Away Win'  # Team B wins
    else:
        return 'Draw'

df['FT Result'] = df.apply(
    lambda row: get_result(row['Team A FT Goals'], row['Team B FT Goals']), 
    axis=1
)

# Also create numeric version (1=Home Win, X=Draw, 2=Away Win)
def get_result_numeric(result):
    if result == 'Home Win':
        return 1
    elif result == 'Draw':
        return 'X'
    elif result == 'Away Win':
        return 2
    return None

df['FT Result (1X2)'] = df['FT Result'].apply(get_result_numeric)

# 2. Total Goals
df['Total Goals'] = df['Team A FT Goals'] + df['Team B FT Goals']

# 3. Over/Under 2.5 Goals
df['Over 2.5'] = df['Total Goals'] > 2.5
df['Under 2.5'] = df['Total Goals'] < 2.5
df['Over 1.5'] = df['Total Goals'] > 1.5
df['Over 3.5'] = df['Total Goals'] > 3.5

# 4. Both Teams Scored (BTTS)
df['BTTS'] = (df['Team A FT Goals'] > 0) & (df['Team B FT Goals'] > 0)

# 5. Goal Difference
df['Goal Difference'] = df['Team A FT Goals'] - df['Team B FT Goals']
df['Absolute Goal Difference'] = abs(df['Goal Difference'])

# 6. HT Result
df['HT Result'] = df.apply(
    lambda row: get_result(row['Team A HT Goals'], row['Team B HT Goals']), 
    axis=1
)

# 7. HT/FT Correlation Pattern
def get_ht_ft_pattern(ht_result, ft_result):
    if pd.isna(ht_result) or pd.isna(ft_result):
        return None
    return f"{ht_result}/{ft_result}"

df['HT/FT Pattern'] = df.apply(
    lambda row: get_ht_ft_pattern(row['HT Result'], row['FT Result']), 
    axis=1
)

# 8. Round Type (NOW USING Round Number)
df['Round Type'] = df['Round Number'].apply(
    lambda x: 'Early (1-10)' if x <= 10 else ('Mid (11-25)' if x <= 25 else 'Late (26-38)') if pd.notna(x) else None
)

# 9. Match Number in Round (1-10)
# Group by round and assign match number
df['Match Number in Round'] = df.groupby('Round Number').cumcount() + 1

print("✓ Basic features created")
print(f"\nFT Result distribution:")
print(df['FT Result'].value_counts())

# ===== Cell 4 =====
# Cell 5: Preview basic features
preview_cols = ['Round Number', 'Team A', 'Team B', 'FT Score', 'Total Goals', 
                'FT Result', 'BTTS', 'Over 2.5', 'HT/FT Pattern', 'Round Type']
df[preview_cols].head(15)

# ===== Cell 5 =====
# Cell 6: Calculate Team Form Metrics (Last 5 games)

def calculate_team_form(df, team_col):
    """Calculate rolling form for a team (last 5 games)"""
    
    # Create a list to store all matches for each team
    team_matches = []
    
    for idx, row in df.iterrows():
        team_name = row[team_col]
        
        # Find all previous matches for this team
        if team_col == 'Team A':
            # Team A previous matches (as home team)
            prev_matches = df[(df['Team A'] == team_name) & (df.index < idx)]
            goals_for = 'Team A FT Goals'
            goals_against = 'Team B FT Goals'
        else:
            # Team B previous matches (as away team)
            prev_matches = df[(df['Team B'] == team_name) & (df.index < idx)]
            goals_for = 'Team B FT Goals'
            goals_against = 'Team A FT Goals'
        
        # Get last 5 matches
        last_5 = prev_matches.tail(5)
        
        if len(last_5) > 0:
            # Calculate metrics
            wins = sum(last_5.apply(
                lambda r: r[goals_for] > r[goals_against], axis=1
            ))
            draws = sum(last_5.apply(
                lambda r: r[goals_for] == r[goals_against], axis=1
            ))
            losses = sum(last_5.apply(
                lambda r: r[goals_for] < r[goals_against], axis=1
            ))
            
            avg_goals_scored = last_5[goals_for].mean()
            avg_goals_conceded = last_5[goals_against].mean()
            
            # Calculate points (3 for win, 1 for draw)
            points = wins * 3 + draws * 1
            
            team_matches.append({
                'games_played': len(last_5),
                'wins': wins,
                'draws': draws,
                'losses': losses,
                'points': points,
                'avg_goals_scored': round(avg_goals_scored, 2),
                'avg_goals_conceded': round(avg_goals_conceded, 2),
                'form_percentage': round((points / (len(last_5) * 3)) * 100, 1)
            })
        else:
            # No previous matches
            team_matches.append({
                'games_played': 0,
                'wins': 0,
                'draws': 0,
                'losses': 0,
                'points': 0,
                'avg_goals_scored': 0,
                'avg_goals_conceded': 0,
                'form_percentage': 0
            })
    
    return pd.DataFrame(team_matches)

print("Calculating Team A form (last 5 games)...")
team_a_form = calculate_team_form(df, 'Team A')
team_a_form.columns = ['Team A ' + col for col in team_a_form.columns]

print("Calculating Team B form (last 5 games)...")
team_b_form = calculate_team_form(df, 'Team B')
team_b_form.columns = ['Team B ' + col for col in team_b_form.columns]

# Add to main dataframe
df = pd.concat([df.reset_index(drop=True), team_a_form, team_b_form], axis=1)

print("✓ Team form metrics calculated")

# ===== Cell 6 =====
# Cell 7: Calculate Head-to-Head History

def calculate_h2h(df):
    """Calculate head-to-head statistics"""
    h2h_stats = []
    
    for idx, row in df.iterrows():
        team_a = row['Team A']
        team_b = row['Team B']
        
        # Find all previous matches between these two teams (both home and away)
        prev_h2h = df[
            (((df['Team A'] == team_a) & (df['Team B'] == team_b)) |
             ((df['Team A'] == team_b) & (df['Team B'] == team_a))) &
            (df.index < idx)
        ]
        
        if len(prev_h2h) > 0:
            # Count wins for each team
            team_a_wins = 0
            team_b_wins = 0
            draws = 0
            
            for _, match in prev_h2h.iterrows():
                if match['Team A'] == team_a:
                    # Team A was home
                    if match['Team A FT Goals'] > match['Team B FT Goals']:
                        team_a_wins += 1
                    elif match['Team A FT Goals'] < match['Team B FT Goals']:
                        team_b_wins += 1
                    else:
                        draws += 1
                else:
                    # Team A was away
                    if match['Team B FT Goals'] > match['Team A FT Goals']:
                        team_a_wins += 1
                    elif match['Team B FT Goals'] < match['Team A FT Goals']:
                        team_b_wins += 1
                    else:
                        draws += 1
            
            total_h2h = len(prev_h2h)
            avg_goals = prev_h2h['Total Goals'].mean()
            
            h2h_stats.append({
                'H2H Matches': total_h2h,
                'H2H Team A Wins': team_a_wins,
                'H2H Team B Wins': team_b_wins,
                'H2H Draws': draws,
                'H2H Avg Goals': round(avg_goals, 2),
                'H2H Team A Win %': round((team_a_wins / total_h2h) * 100, 1) if total_h2h > 0 else 0
            })
        else:
            # No previous H2H
            h2h_stats.append({
                'H2H Matches': 0,
                'H2H Team A Wins': 0,
                'H2H Team B Wins': 0,
                'H2H Draws': 0,
                'H2H Avg Goals': 0,
                'H2H Team A Win %': 0
            })
    
    return pd.DataFrame(h2h_stats)

print("Calculating Head-to-Head statistics...")
h2h_df = calculate_h2h(df)

# Add to main dataframe
df = pd.concat([df.reset_index(drop=True), h2h_df], axis=1)

print("✓ Head-to-head statistics calculated")

# ===== Cell 7 =====
# Cell 8: Preview all new features
print("All Features Created:")
print("="*60)
print("\nBasic Features:")
print("- FT Result, FT Result (1X2)")
print("- Total Goals, Over/Under 2.5, Over 1.5, Over 3.5, BTTS")
print("- Goal Difference, Absolute Goal Difference")
print("- HT Result, HT/FT Pattern")
print("- Round Type, Match Number in Round")

print("\nTeam A Form (Last 5):")
print([col for col in df.columns if 'Team A' in col and 'form' in col.lower() or 'avg' in col.lower()])

print("\nTeam B Form (Last 5):")
print([col for col in df.columns if 'Team B' in col and 'form' in col.lower() or 'avg' in col.lower()])

print("\nHead-to-Head:")
print([col for col in df.columns if 'H2H' in col])

print(f"\n{'='*60}")
print(f"Total columns: {len(df.columns)}")
print(f"Total matches: {len(df)}")

# ===== Cell 8 =====
# Cell 9: Display sample with key features
key_features = [
    'Round Number', 'Match Number in Round', 'Team A', 'Team B', 
    'FT Score', 'FT Result', 'Total Goals', 'Over 2.5', 'BTTS',
    'Team A form_percentage', 'Team B form_percentage',
    'H2H Matches', 'H2H Team A Win %', 'Round Type'
]

df[key_features].head(20)

# ===== Cell 9 =====
# Cell 10: Summary statistics
print("SUMMARY STATISTICS")
print("="*60)

print("\n1. FT Results Distribution:")
print(df['FT Result'].value_counts())
print(f"\n   Percentage:")
print(df['FT Result'].value_counts(normalize=True) * 100)

print("\n2. Goals Distribution:")
print(f"   Average Total Goals: {df['Total Goals'].mean():.2f}")
print(f"   Over 2.5 Goals: {df['Over 2.5'].sum()} matches ({(df['Over 2.5'].sum()/len(df))*100:.1f}%)")
print(f"   BTTS: {df['BTTS'].sum()} matches ({(df['BTTS'].sum()/len(df))*100:.1f}%)")

print("\n3. HT/FT Patterns:")
print(df['HT/FT Pattern'].value_counts().head(10))

print("\n4. Round Type Distribution:")
print(df['Round Type'].value_counts())

print(f"\n{'='*60}")

# ===== Cell 10 =====
# Cell 11: Save enhanced data
output_filename = 'Football_Data_With_Features.xlsx'
df.to_excel(output_filename, index=False)

print(f"✓ Enhanced data saved to: {output_filename}")
print(f"✓ Total features: {len(df.columns)}")
print(f"✓ Total matches: {len(df)}")
print(f"\nColumn names:")
for i, col in enumerate(df.columns, 1):
    print(f"{i:2d}. {col}")
