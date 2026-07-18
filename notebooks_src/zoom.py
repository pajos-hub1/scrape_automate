# Converted from: zoom.ipynb

# ===== Cell 0 =====
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import pandas as pd
import time
from bs4 import BeautifulSoup
import re
import os

print("Starting browser...")

# Auto-downloads the correct ChromeDriver for your Chrome version
service = Service(ChromeDriverManager().install())
driver = webdriver.Chrome(service=service)

url = 'https://zoomapi.bet9ja.com/zoom/results/premier-zoom?matchId=2462420&clientId=68&offset=3600000'
driver.get(url)
time.sleep(3)
print("✓ Browser loaded successfully!")

# ===== Cell 1 =====
print("="*80)
print("FIXED - FINDING THE ACTIVE TABLE")
print("="*80)

def get_active_table():
    """Find the table that's actually visible/active in the carousel"""
    try:
        # Wait for tables to be present
        tables = WebDriverWait(driver, 10).until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, 'table.l-table.mt10'))
        )
        
        print(f"   Found {len(tables)} tables on screen")
        
        # Find the visible one (check if it's displayed and has size)
        for i, table in enumerate(tables):
            if table.is_displayed() and table.size['width'] > 0:
                # Additional check: get its position to see if it's in viewport center
                location = table.location
                size = table.size
                
                # The active table should be centered (not off-screen)
                # This is a heuristic - adjust if needed
                if location['x'] >= 0 and location['x'] < 2000:  # Rough check
                    print(f"   Using table index {i} (visible and positioned)")
                    return table
        
        # Fallback: use middle table if all else fails
        print(f"   Fallback: using table index 1")
        return tables[1] if len(tables) > 1 else tables[0]
        
    except Exception as e:
        print(f"Error getting table: {e}")
        return None

def scrape_active_table():
    """Scrape the active table"""
    table = get_active_table()
    
    if not table:
        return None, None
    
    try:
        rows = table.find_elements(By.TAG_NAME, 'tr')
        
        # Get HTML
        html_rows = []
        for row in rows:
            html_rows.append(row.get_attribute('outerHTML'))
        
        # Parse with BeautifulSoup
        soup = BeautifulSoup('\n'.join(html_rows), 'html.parser')
        rows = soup.find_all('tr')
        
        teams = []
        scores = []
        
        for row in rows:
            team_names = row.find_all('span', class_='tbl-team-name')
            score_cells = row.find_all('td', class_='txt-y')
            
            teams.extend([name.get_text(strip=True) for name in team_names])
            scores.extend([score.get_text(strip=True) for score in score_cells])
        
        teams_list = [teams[i:i+2] for i in range(0, len(teams), 2)]
        scores_list = [scores[i:i+2] for i in range(0, len(scores), 2)]
        
        return teams_list, scores_list
    except Exception as e:
        print(f"Error scraping: {e}")
        return None, None

all_rounds_data = []
scraped_rounds = set()
seen_rounds = set()

try:
    max_attempts = 50
    attempt = 0
    
    while attempt < max_attempts:
        attempt += 1
        print(f"\n--- Attempt {attempt} ---")
        
        # Get round number from carousel text
        try:
            round_no = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, '.carousel__control-txt'))
            )
            round_text = round_no.text
            
            round_match = re.search(r'Round\s+(\d+)', round_text)
            if round_match:
                current_round = int(round_match.group(1))
            else:
                print("Could not extract round number")
                break
            
            print(f"Carousel: {round_text}")
            
            # Check if looped back
            if current_round in seen_rounds:
                print(f"⚠️ Looped back to Round {current_round} - done!")
                print(f"Scraped {len(scraped_rounds)} rounds total")
                break
            
            seen_rounds.add(current_round)
            
        except Exception as e:
            print(f"Error: {e}")
            break
        
        # Wait a moment for carousel animation to finish
        time.sleep(2)
        
        # Scrape the ACTIVE table
        print(f"🆕 Scraping Round {current_round}...")
        
        teams_list, scores_list = scrape_active_table()
        
        if teams_list and scores_list and len(teams_list) > 0:
            df = pd.DataFrame({
                'Team A': [t[0] for t in teams_list],
                'Team B': [t[1] for t in teams_list],
                'FT Score': [s[0] for s in scores_list],
                'HT Score': [s[1] for s in scores_list],
            })
            
            filename = f'Round {current_round}.xlsx'
            df.to_excel(filename, sheet_name=f'Round {current_round}', index=False)
            
            first_match = f"{teams_list[0][0]} vs {teams_list[0][1]}"
            print(f"✓ Saved {filename}")
            print(f"  First match: {first_match}")
            
            df['Round'] = f'Round {current_round}'
            df['Round Number'] = current_round
            all_rounds_data.append(df)
            
            scraped_rounds.add(current_round)
        else:
            print("⚠️ No data found")
        
        # Click next
        try:
            next_button = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, '.carousel__control-right'))
            )
            next_button.click()
            print("   Clicked next →")
            
            # Wait for carousel to finish animating
            time.sleep(3)
            
        except Exception as e:
            print(f"Could not click next: {e}")
            break
    
    # Save combined
    if all_rounds_data:
        final_combined = pd.concat(all_rounds_data, ignore_index=True)
        final_combined = final_combined.sort_values('Round Number').reset_index(drop=True)
        final_combined.to_excel('All_Rounds_Combined_s37.xlsx', index=False)
        
        print(f"\n{'='*80}")
        print(f"✓ SUCCESS!")
        print(f"{'='*80}")
        print(f"Rounds: {sorted(scraped_rounds)}")
        print(f"Total matches: {len(final_combined)}")
        print(f"Saved: All_Rounds_Combined_s37.xlsx")
        print(f"{'='*80}")
    
finally:
    print("\nDone!")

# ===== Cell 2 =====
driver.quit()
