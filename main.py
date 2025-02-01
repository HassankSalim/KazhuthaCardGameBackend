from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from typing import Dict, List, Optional, Set
from pydantic import BaseModel
import uuid
import asyncio
from dataclasses import dataclass, asdict
from enum import Enum
import json
from datetime import datetime, timedelta
import weakref
import random

# Data Models
class Suit(str, Enum):
    HEARTS = "HEARTS"
    DIAMONDS = "DIAMONDS"
    CLUBS = "CLUBS"
    SPADES = "SPADES"

class Rank(str, Enum):
    ACE = "ACE"
    KING = "KING"
    QUEEN = "QUEEN"
    JACK = "JACK"
    TEN = "10"
    NINE = "9"
    EIGHT = "8"
    SEVEN = "7"
    SIX = "6"
    FIVE = "5"
    FOUR = "4"
    THREE = "3"
    TWO = "2"

@dataclass
class Card:
    suit: Suit
    rank: Rank

    @property
    def value(self) -> int:
        rank_values = {
            Rank.ACE: 14, Rank.KING: 13, Rank.QUEEN: 12, Rank.JACK: 11,
            Rank.TEN: 10, Rank.NINE: 9, Rank.EIGHT: 8, Rank.SEVEN: 7,
            Rank.SIX: 6, Rank.FIVE: 5, Rank.FOUR: 4, Rank.THREE: 3, Rank.TWO: 2
        }
        return rank_values[self.rank]

# Request/Response Models
class CreateGameRequest(BaseModel):
    player_name: str

class JoinGameRequest(BaseModel):
    game_id: str
    player_name: str

class StartGameRequest(BaseModel):
    game_id: str
    player_name: str

class PlayCardRequest(BaseModel):
    game_id: str
    player_name: str
    card: dict  # {suit: str, rank: str}

class GameStateResponse(BaseModel):
    game_id: str
    players: List[dict]
    current_player: str
    current_suit: Optional[str]
    current_pile: List[dict]
    game_state: str
    winner: Optional[str]

# WebSocket Manager
class ConnectionManager:
    def __init__(self):
        # Store connection details with timestamps
        self.active_connections: Dict[str, Dict[str, dict]] = {}
        # Keep track of ping tasks
        self.ping_tasks: Dict[str, asyncio.Task] = {}
        # Cleanup task
        self.cleanup_task: Optional[asyncio.Task] = None
        # Connection timeouts (in seconds)
        self.PING_INTERVAL = 30
        self.CONNECTION_TIMEOUT = 60
        self.CLEANUP_INTERVAL = 300  # 5 minutes

    async def connect(self, websocket: WebSocket, game_id: str, player_name: str):
        try:
            await websocket.accept()
            
            # Initialize game connections if not exists
            if game_id not in self.active_connections:
                self.active_connections[game_id] = {}
            
            # Store connection with timestamp
            self.active_connections[game_id][player_name] = {
                'websocket': websocket,
                'last_seen': datetime.now(),
                'connected': True
            }

            # Start ping task for this connection
            self.ping_tasks[f"{game_id}_{player_name}"] = asyncio.create_task(
                self._keep_alive(game_id, player_name)
            )

            # Start cleanup task if not running
            if not self.cleanup_task or self.cleanup_task.done():
                self.cleanup_task = asyncio.create_task(self._cleanup_connections())

        except Exception as e:
            print(f"Error in connect: {str(e)}")
            await self._handle_disconnect(game_id, player_name)

    async def disconnect(self, game_id: str, player_name: str):
        await self._handle_disconnect(game_id, player_name)

    async def _handle_disconnect(self, game_id: str, player_name: str):
        # Cancel ping task
        task_key = f"{game_id}_{player_name}"
        if task_key in self.ping_tasks:
            self.ping_tasks[task_key].cancel()
            del self.ping_tasks[task_key]

        # Remove connection
        if game_id in self.active_connections:
            if player_name in self.active_connections[game_id]:
                conn_info = self.active_connections[game_id][player_name]
                if 'websocket' in conn_info:
                    try:
                        await conn_info['websocket'].close()
                    except Exception:
                        pass
                del self.active_connections[game_id][player_name]

            # Remove game if no players
            if not self.active_connections[game_id]:
                del self.active_connections[game_id]

    async def broadcast_to_game(self, game_id: str, message: dict):
        if game_id not in self.active_connections:
            return

        game = games.get(game_id)  # Get the game instance
        if not game:
            return

        disconnected_players = []
        for player_name, conn_info in self.active_connections[game_id].items():
            try:
                if conn_info['connected']:
                    # Create a player-specific message with their hand
                    player_message = message.copy()
                    if 'game_state' in player_message:
                        player_message['game_state'] = game.get_state(player_name)
                    
                    await conn_info['websocket'].send_json(player_message)
                    conn_info['last_seen'] = datetime.now()
            except Exception:
                disconnected_players.append(player_name)

        # Clean up disconnected players
        for player_name in disconnected_players:
            await self._handle_disconnect(game_id, player_name)

    async def _keep_alive(self, game_id: str, player_name: str):
        """Send periodic ping messages to keep connection alive"""
        try:
            while True:
                if (game_id in self.active_connections and 
                    player_name in self.active_connections[game_id]):
                    conn_info = self.active_connections[game_id][player_name]
                    try:
                        await conn_info['websocket'].send_json({"type": "ping"})
                        conn_info['last_seen'] = datetime.now()
                    except Exception:
                        await self._handle_disconnect(game_id, player_name)
                        break
                else:
                    break
                await asyncio.sleep(self.PING_INTERVAL)
        except asyncio.CancelledError:
            # Task was cancelled, clean up
            await self._handle_disconnect(game_id, player_name)

    async def _cleanup_connections(self):
        """Periodically clean up stale connections"""
        while True:
            try:
                current_time = datetime.now()
                games_to_check = list(self.active_connections.keys())

                for game_id in games_to_check:
                    if game_id not in self.active_connections:
                        continue

                    players = list(self.active_connections[game_id].keys())
                    for player_name in players:
                        if game_id not in self.active_connections:
                            break

                        if player_name not in self.active_connections[game_id]:
                            continue

                        conn_info = self.active_connections[game_id][player_name]
                        time_since_last_seen = current_time - conn_info['last_seen']

                        if time_since_last_seen > timedelta(seconds=self.CONNECTION_TIMEOUT):
                            await self._handle_disconnect(game_id, player_name)

                await asyncio.sleep(self.CLEANUP_INTERVAL)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Error in cleanup: {str(e)}")
                await asyncio.sleep(self.CLEANUP_INTERVAL)
# Game Logic
class GameSession:
    def __init__(self, game_id: str, host_name: str):
        self.game_id = game_id
        self.players: Dict[str, List[Card]] = {host_name: []}
        self.player_order: List[str] = [host_name]
        self.current_player_idx = 0
        self.current_pile: List[tuple[str, Card]] = []
        self.current_suit: Optional[Suit] = None
        self.game_started = False
        self.finished = False
        self.winner = None
        self.host_name = host_name
        self.original_suit: Optional[Suit] = None  # Track the suit that started the round
        self.discarded_cards: List[Card] = []  # Cards set aside after each round

    def add_player(self, player_name: str) -> bool:
        if player_name in self.players or self.game_started:
            return False
        self.players[player_name] = []
        self.player_order.append(player_name)
        return True

    def start_game(self):
        if len(self.players) < 2:
            raise ValueError("Need at least 2 players")
        
        # Create and deal deck
        deck = self._create_deck()
        cards_per_player = len(deck) // len(self.players)
        
        for i, player_name in enumerate(self.player_order):
            start_idx = i * cards_per_player
            end_idx = start_idx + cards_per_player
            self.players[player_name] = deck[start_idx:end_idx]

        # Find Ace of Spades
        ace_of_spades = Card(Suit.SPADES, Rank.ACE)
        for i, player_name in enumerate(self.player_order):
            if ace_of_spades in self.players[player_name]:
                self.current_player_idx = i
                break

        self.game_started = True

    def _create_deck(self) -> List[Card]:
        # Create initial deck
        deck = []
        for suit in Suit:
            for rank in Rank:
                deck.append(Card(suit, rank))
        
        # Shuffle the deck multiple times
        for _ in range(7):  # Standard is to shuffle 7 times
            random.shuffle(deck)
        
        return deck

    def play_card(self, player_name: str, card_dict: dict) -> bool:
        if not self.game_started or self.finished:
            return False

        if player_name != self.current_player:
            return False

        card = Card(Suit(card_dict['suit']), Rank(card_dict['rank']))
        player_hand = self.players[player_name]

        # Validate card is in hand
        if card not in player_hand:
            return False

        # First card of the game (not round) must be Ace of Spades
        if not self.current_pile and len(self.discarded_cards) == 0:
            if card.suit != Suit.SPADES or card.rank != Rank.ACE:
                return False

        # If this is the first card of a new round, set it as the original suit
        if not self.current_pile:
            self.original_suit = card.suit
            self.current_suit = card.suit

        # Must follow suit if possible
        if (self.current_suit and
                any(c.suit == self.current_suit for c in player_hand) and
                card.suit != self.current_suit):
            return False

        # Play card
        player_hand.remove(card)
        self.current_pile.append((player_name, card))

        # Check if a different suit was played (when following suit was not possible)
        if card.suit != self.current_suit:
            # End the round immediately and give cards to player with highest card
            self._resolve_round()
        else:
            # Check if round is complete (all active players have played)
            active_players = [p for p in self.players.items() if p[1]]  # Players still with cards
            if len(self.current_pile) == len(active_players):
                self._resolve_round()
            else:
                self._next_player()

        # Check if any player has run out of cards
        for player, cards in self.players.items():
            if not cards:  # Player has no cards left
                if len([p for p in self.players.values() if p]):  # If others still have cards
                    self._next_player()  # Skip this player in future rounds
                else:  # Game is over
                    self.finished = True
                    # Last player with cards is the Bhabhi
                    remaining_players = [(p, c) for p, c in self.players.items() if c]
                    if remaining_players:
                        self.winner = remaining_players[0][0]  # This is the Bhabhi

        return True

    def _resolve_round(self):
        if not self.current_pile:
            return

        # Find highest card of original suit
        original_suit_cards = [(pname, card) for pname, card in self.current_pile
                               if card.suit == self.original_suit]

        # Find the winning player (highest card of original suit)
        winning_play = max(original_suit_cards, key=lambda x: x[1].value)
        winning_player = winning_play[0]

        # Check if all cards match the original suit
        all_same_suit = all(card.suit == self.original_suit for _, card in self.current_pile)

        if all_same_suit:
            # If all cards are the same suit, move them to discarded pile
            self.discarded_cards.extend(card for _, card in self.current_pile)
        else:
            # If different suits exist, give all cards to the winner
            self.players[winning_player].extend(card for _, card in self.current_pile)

        # Clear pile and suits
        self.current_pile = []
        self.current_suit = None
        self.original_suit = None

        # Set winner as next player to start new round
        self.current_player_idx = self.player_order.index(winning_player)

    def _next_player(self):
        initial_idx = self.current_player_idx
        while True:
            self.current_player_idx = (self.current_player_idx + 1) % len(self.player_order)
            # Check if this player still has cards
            if self.players[self.current_player]:
                break
            # If we've checked all players, break to avoid infinite loop
            if self.current_player_idx == initial_idx:
                break

    @property
    def current_player(self) -> str:
        return self.player_order[self.current_player_idx]

    def get_state(self, player_name: Optional[str] = None) -> dict:
        """
        Get the current game state. If player_name is provided,
        include that player's hand in the response.
        """
        base_state = {
            'game_id': self.game_id,
            'players': [
                {
                    'name': name,
                    'card_count': len(cards),
                    'is_host': name == self.host_name
                }
                for name, cards in self.players.items()
            ],
            'current_player': self.current_player,
            'current_suit': self.current_suit.value if self.current_suit else None,
            'current_pile': [
                {'player': pname, 'card': {'suit': card.suit.value, 'rank': card.rank.value}}
                for pname, card in self.current_pile
            ],
            'game_state': 'FINISHED' if self.finished else 'PLAYING' if self.game_started else 'WAITING',
            'winner': self.winner
        }
        
        # If a player_name is provided and they're in the game,
        # include their hand in the response
        if player_name and player_name in self.players:
            base_state['your_hand'] = [
                {'suit': card.suit.value, 'rank': card.rank.value}
                for card in self.players[player_name]
            ]
        
        return base_state

# FastAPI App
app = FastAPI(title="Bhabi Game Server")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, replace with actual origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Game state storage
games: Dict[str, GameSession] = {}
manager = ConnectionManager()

# REST Endpoints
@app.post("/game/create")
async def create_game(request: CreateGameRequest):
    game_id = str(uuid.uuid4())[:8].upper()
    games[game_id] = GameSession(game_id, request.player_name)
    return {"game_id": game_id}

@app.post("/game/join")
async def join_game(request: JoinGameRequest):
    if request.game_id not in games:
        raise HTTPException(status_code=404, message="Game not found")
    
    game = games[request.game_id]
    if not game.add_player(request.player_name):
        raise HTTPException(status_code=400, message="Cannot join game")
    
    await manager.broadcast_to_game(request.game_id, {
        "type": "player_joined",
        "game_state": game.get_state()
    })
    
    return {"success": True}

@app.post("/game/start")
async def start_game(request: StartGameRequest):
    game_id = request.game_id
    player_name = request.player_name
    if game_id not in games:
        raise HTTPException(status_code=404, message="Game not found")
    
    game = games[game_id]
    if game.host_name != player_name:
        raise HTTPException(status_code=403, message="Only host can start game")
    
    try:
        game.start_game()
        await manager.broadcast_to_game(game_id, {
            "type": "game_started",
            "game_state": game.get_state()
        })
        return {"success": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/game/play")
async def play_card(request: PlayCardRequest):
    if request.game_id not in games:
        raise HTTPException(status_code=404, message="Game not found")
    
    game = games[request.game_id]
    success = game.play_card(request.player_name, request.card)
    
    if success:
        # Get updated game state for each player
        game_state = game.get_state(request.player_name)
        await manager.broadcast_to_game(request.game_id, {
            "type": "card_played",
            "player": request.player_name,
            "card": request.card,
            "game_state": game_state
        })
        return {"success": True, "game_state": game_state}
    
    raise HTTPException(status_code=400, detail="Invalid move")

def get_connection_manager():
    # Using a global variable for the connection manager
    if not hasattr(get_connection_manager, 'manager'):
        get_connection_manager.manager = ConnectionManager()
    return get_connection_manager.manager

# WebSocket endpoint
@app.websocket("/ws/{game_id}/{player_name}")
async def websocket_endpoint(
    websocket: WebSocket,
    game_id: str,
    player_name: str,
):

    await manager.connect(websocket, game_id, player_name)
    try:
        while True:
            try:
                # Wait for messages with timeout
                data = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=manager.CONNECTION_TIMEOUT
                )
                
                # Handle pong responses
                if data == "pong":
                    if (game_id in manager.active_connections and 
                        player_name in manager.active_connections[game_id]):
                        manager.active_connections[game_id][player_name]['last_seen'] = datetime.now()
                    continue

                # Handle other messages...
                # Add your game message handling logic here

            except asyncio.TimeoutError:
                # Connection timed out
                await manager.disconnect(game_id, player_name)
                break

    except WebSocketDisconnect:
        await manager.disconnect(game_id, player_name)

@app.get("/healthz")
async def health_check():
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat()
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)